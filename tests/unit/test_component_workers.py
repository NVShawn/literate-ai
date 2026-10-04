"""Exact routing, public scope, and real byte custody across worker stores."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.component_artifact_custody import import_component_artifacts
from literate_ai.adapters.component_worker_dispatch import RoutedComponentNodeDispatcher
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_workers import (
    ComponentNodeDispatchOutcome,
    ComponentNodeRecoveryCandidate,
    ComponentWorkerError,
    component_artifact_handoff,
    component_node_payload_result,
    plan_component_worker_routing,
    validate_component_artifact_import,
    validate_component_worker_routing,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.component_workers import (
    ComponentArtifactImportReceipt,
    ComponentWorkerProduct,
)
from literate_ai.contracts.executable_components import ArtifactExport
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.storage.cas import BlobIntegrityError, BlobNotFoundError, FileSystemCAS
from tests.support.fixtures_test_component_execution_planning import _diamond_lock, _models
from tests.support.fixtures_test_component_lock_contracts import identity
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_standard_project_lifecycle import (
    LifecyclePorts,
    _decision,
    _names,
    _prepared_execution,
    _prepared_nodes,
    _service,
)


class _TransportHandler:
    def __init__(self, *, overlap=(), corrupt_worker=False):
        self.overlap = set(overlap)
        self.barrier = threading.Barrier(2) if self.overlap else None
        self.corrupt_worker = corrupt_worker
        self.started = []
        self.cancelled = []
        self.outcomes = {}

    def execute(self, worker, request, execute, *, cancellation):
        revision = request.assignment.component_revision.uri
        self.started.append(revision)
        if revision in self.overlap:
            self.barrier.wait(timeout=5)
        payload = execute()
        result = component_node_payload_result(payload)
        receipt = ComponentArtifactImportReceipt(
            request.handoff_identity,
            worker.identity,
            tuple(
                sorted(
                    {
                        *request.provider_artifact_identities,
                        *request.package_artifact_identities,
                    },
                    key=lambda item: item.uri,
                )
            ),
        )
        outcome = ComponentNodeDispatchOutcome(
            request.identity,
            identity("wrong-worker") if self.corrupt_worker else worker.identity,
            result.identity,
            receipt,
            payload,
        )
        self.outcomes[revision] = outcome
        return outcome

    def cancel(self, worker, request):
        self.cancelled.append(request.assignment.component_revision.uri)


class ComponentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.lock = _diamond_lock(include_invoice_money_packaging_edge=True)
        self.plan = plan_component_execution(
            self.lock, model_identities=_models(self.lock)
        )
        self.catalog = ExecutionWorkerCatalog(
            (
                ExecutionWorker("alpha", ExecutionWorkerKind.LOCAL),
                ExecutionWorker("beta", ExecutionWorkerKind.LOCAL),
            )
        )
        self.nodes = {node.revision.coordinate.name: node for node in self.lock.nodes}
        self.routes = {
            node.revision.identity.uri: ("alpha" if name == "money" else "beta")
            for name, node in self.nodes.items()
        }
        self.routing = plan_component_worker_routing(
            self.plan, self.lock, self.catalog, self.routes
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = FileSystemCAS(Path(self.temp.name) / "alpha")
        self.destination = FileSystemCAS(Path(self.temp.name) / "beta")
        self.product = self._product("money", b"provider-native-artifact\x00\xff")
        self.handoff = self._handoff(
            {self.product.component_revision.uri: self.product}
        )

    def _product(self, name, content):
        node = self.nodes[name]
        worker = self.catalog.worker(self.routes[node.revision.identity.uri])
        blob = self.source.put_bytes(content)
        export = ArtifactExport(
            export_id="library",
            component_revision=node.revision.identity,
            role="library",
            abi_identity=identity("native-abi"),
            target_identity=identity("native-target"),
            media_type=blob.media_type,
            producer_identity=identity("producer"),
            source_tree_identity=identity("source"),
            toolchain_identity=identity("toolchain"),
            authorization_identity=identity("authorization"),
            dependency_artifact_identities=(),
            blob=blob,
        )
        return ComponentWorkerProduct(
            node.revision.identity,
            worker.identity,
            identity("accepted-" + name),
            (export,),
        )

    def _handoff(self, products, name="invoice-cli"):
        return component_artifact_handoff(
            self.routing, self.plan, self.nodes[name].revision.identity, products
        )

    def _import(self, **overrides):
        arguments = dict(
            worker_identity=self.handoff.consumer.worker_identity,
            sources={self.product.worker_identity.uri: self.source},
            destination=self.destination,
        )
        arguments.update(overrides)
        return import_component_artifacts(self.handoff, **arguments)

    def test_contract_round_trips_and_rejects_unknown_fields(self):
        receipt = self._import()
        schemas = SchemaCatalog()
        for value in (
            self.routing.assignments[0],
            self.routing,
            self.product,
            self.handoff,
            receipt,
        ):
            with self.subTest(contract=type(value).__name__):
                payload = json.loads(json.dumps(value.to_dict()))
                schemas.validate(value.SCHEMA, payload)
                self.assertEqual(type(value).from_dict(payload), value)
                payload["private_workspace"] = "/not-authority"
                with self.assertRaises(ContractValidationError):
                    type(value).from_dict(payload)

    def test_malformed_collections_fail_as_contract_errors(self):
        for value, field in (
            (self.routing, "assignments"),
            (self.product, "exports"),
            (self.handoff, "predecessors"),
            (self._import(), "export_identities"),
        ):
            for invalid in (None, "bad", 1, {}):
                with self.subTest(contract=type(value).__name__, invalid=invalid):
                    with self.assertRaises(ContractValidationError):
                        replace(value, **{field: invalid})
                    payload = value.to_dict()
                    payload[field] = invalid
                    with self.assertRaises(ContractValidationError):
                        type(value).from_dict(payload)

    def test_route_binds_all_exact_workers_and_locked_selections(self):
        validate_component_worker_routing(
            self.routing, self.plan, self.lock, self.catalog
        )
        for assignment in self.routing.assignments:
            node = next(
                item
                for item in self.lock.nodes
                if item.revision.identity == assignment.component_revision
            )
            self.assertEqual(
                assignment.target_flavor_selection_identity,
                node.target_flavor_selection.identity,
            )
        changed = replace(
            self.routing.assignments[0], worker_identity=identity("other-worker")
        )
        stale = replace(
            self.routing, assignments=(changed, *self.routing.assignments[1:])
        )
        with self.assertRaises(ComponentWorkerError):
            validate_component_worker_routing(stale, self.plan, self.lock, self.catalog)

    def test_missing_extra_and_unknown_worker_routes_fail(self):
        first = next(iter(self.routes))
        for routes in (
            {key: value for key, value in self.routes.items() if key != first},
            {**self.routes, identity("foreign").uri: "alpha"},
            {**self.routes, first: "unknown"},
        ):
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                plan_component_worker_routing(
                    self.plan, self.lock, self.catalog, routes
                )

    def test_changed_catalog_or_target_fails_revalidation(self):
        changed_catalog = ExecutionWorkerCatalog(
            (*self.catalog.workers, ExecutionWorker("gamma", ExecutionWorkerKind.LOCAL))
        )
        with self.assertRaises(ComponentWorkerError):
            validate_component_worker_routing(
                self.routing, self.plan, self.lock, changed_catalog
            )
        other_target = ExecutionWorkerCatalog(
            tuple(
                replace(worker, target_profile="other")
                for worker in self.catalog.workers
            )
        )
        with self.assertRaises(ComponentWorkerError):
            plan_component_worker_routing(
                self.plan, self.lock, other_target, self.routes
            )

    def test_changed_plan_or_lock_fails(self):
        with self.assertRaises(ComponentWorkerError):
            validate_component_worker_routing(
                self.routing,
                replace(self.plan, planner_identity=identity("new-planner")),
                self.lock,
                self.catalog,
            )
        with self.assertRaises(ComponentWorkerError):
            plan_component_worker_routing(
                self.plan,
                _diamond_lock(money_spec="changed"),
                self.catalog,
                self.routes,
            )

    def test_public_routing_has_no_private_worker_fields(self):
        payload = json.dumps(self.routing.to_dict())
        for field in (
            "endpoint",
            "workspace",
            "environment",
            "command",
            "lifecycle_executable",
        ):
            self.assertNotIn('"' + field + '"', payload)
        with self.assertRaises(ContractValidationError):
            replace(self.routing.assignments[0], worker_id="ssh://private-host")

    def test_duplicate_and_reordered_routes_fail(self):
        for assignments in (
            self.routing.assignments[::-1],
            self.routing.assignments * 2,
            (),
        ):
            with self.assertRaises(ContractValidationError):
                replace(self.routing, assignments=assignments)

    def test_only_package_predecessor_is_transferred(self):
        unrelated = self._product(
            "reporting", b"private-generation-only-implementation"
        )
        handoff = self._handoff(
            {
                self.product.component_revision.uri: self.product,
                unrelated.component_revision.uri: unrelated,
            }
        )
        self.assertEqual(handoff.predecessors, (self.product,))
        self.assertEqual(self._handoff({}, "pricing").predecessors, ())
        self._import()
        self.assertFalse(self.destination.contains(unrelated.exports[0].blob))

    def test_build_runtime_toolchain_handoffs_select_direct_diamond_providers(self):
        for kind in (
            DependencyKind.BUILD,
            DependencyKind.RUNTIME,
            DependencyKind.TOOLCHAIN,
        ):
            with self.subTest(kind=kind):
                self.lock = _diamond_lock(dependency_kind=kind)
                self.plan = plan_component_execution(
                    self.lock, model_identities=_models(self.lock)
                )
                self.nodes = {
                    node.revision.coordinate.name: node for node in self.lock.nodes
                }
                self.routes = {
                    node.revision.identity.uri: "alpha" for node in self.lock.nodes
                }
                self.routes[self.lock.root_revision.uri] = "beta"
                self.routing = plan_component_worker_routing(
                    self.plan, self.lock, self.catalog, self.routes
                )
                products = [
                    self._product(name, name.encode())
                    for name in ("money", "pricing", "reporting")
                ]
                handoff = self._handoff(
                    {product.component_revision.uri: product for product in products}
                )
                self.assertEqual(
                    {product.component_revision for product in handoff.predecessors},
                    {
                        self.nodes[name].revision.identity
                        for name in ("pricing", "reporting")
                    },
                )
                receipt = import_component_artifacts(
                    handoff,
                    worker_identity=handoff.consumer.worker_identity,
                    sources={self.catalog.worker("alpha").identity.uri: self.source},
                    destination=self.destination,
                )
                validate_component_artifact_import(handoff, receipt)
                self.assertEqual(len(receipt.export_identities), 2)

    def test_missing_or_wrong_predecessor_fails(self):
        for products in (
            {},
            {
                self.product.component_revision.uri: replace(
                    self.product, worker_identity=identity("wrong")
                )
            },
            {
                self.product.component_revision.uri: self._product(
                    "reporting", b"unrelated"
                )
            },
        ):
            with self.assertRaises(ComponentWorkerError):
                self._handoff(products)

    def test_product_cannot_relabel_foreign_exports_or_feed_itself(self):
        foreign = self._product("reporting", b"foreign")
        with self.assertRaises(ContractValidationError):
            replace(self.product, exports=foreign.exports)
        with self.assertRaises(ContractValidationError):
            replace(self.product, exports=self.product.exports * 2)
        consumer_product = self._product("invoice-cli", b"consumer")
        with self.assertRaises(ContractValidationError):
            replace(self.handoff, predecessors=(consumer_product,))

    def test_foreign_consumer_or_plan_fails(self):
        with self.assertRaises(ComponentWorkerError):
            component_artifact_handoff(self.routing, self.plan, identity("foreign"), {})
        with self.assertRaises(ComponentWorkerError):
            component_artifact_handoff(
                self.routing,
                replace(self.plan, planner_identity=identity("new")),
                self.lock.root_revision,
                {},
            )

    def test_transfers_real_bytes_and_preserves_all_export_metadata(self):
        receipt = self._import()
        validate_component_artifact_import(self.handoff, receipt)
        export = self.product.exports[0]
        self.assertEqual(
            self.destination.get_bytes(export.blob), b"provider-native-artifact\x00\xff"
        )
        self.assertEqual(receipt.export_identities, (export.identity,))
        self.assertEqual(self.handoff.predecessors[0].exports[0], export)
        self.assertEqual(self._import(), receipt)

    def test_corrupt_source_or_destination_never_returns_receipt(self):
        blob = self.product.exports[0].blob
        original = self.source.get_bytes(blob)
        self.source.path_for(blob).write_bytes(b"x" * blob.size)
        with self.assertRaises(BlobIntegrityError):
            self._import()
        self.source.path_for(blob).write_bytes(original)
        self._import()
        self.destination.path_for(blob).write_bytes(b"y" * blob.size)
        with self.assertRaises(BlobIntegrityError):
            self._import()

    def test_size_drift_missing_blob_and_wrong_destination_fail(self):
        with self.assertRaises(ComponentWorkerError):
            self._import(worker_identity=identity("wrong-worker"))
        with self.assertRaises(ComponentWorkerError):
            self._import(sources={})
        self.source.path_for(self.product.exports[0].blob).write_bytes(b"short")
        with self.assertRaises(BlobIntegrityError):
            self._import()
        self.source.path_for(self.product.exports[0].blob).unlink()
        with self.assertRaises(BlobNotFoundError):
            self._import()

    def test_source_changed_after_verification_fails(self):
        original_verify = self.source.verify

        def change_after_verify(blob):
            original_verify(blob)
            self.source.path_for(blob).write_bytes(b"changed-between-check-and-copy")

        with patch.object(self.source, "verify", side_effect=change_after_verify):
            with self.assertRaises(BlobIntegrityError):
                self._import()

    def test_destination_must_verify_after_copy(self):
        original_put = self.destination.put_file

        def corrupt_after_copy(*args, **kwargs):
            blob = original_put(*args, **kwargs)
            self.destination.path_for(blob).write_bytes(b"x" * blob.size)
            return blob

        with patch.object(self.destination, "put_file", side_effect=corrupt_after_copy):
            with self.assertRaises(BlobIntegrityError):
                self._import()

    def test_receipt_replay_fails(self):
        receipt = self._import()
        for invalid in (
            replace(receipt, handoff_identity=identity("old-handoff")),
            replace(receipt, worker_identity=identity("other-worker")),
            replace(receipt, export_identities=()),
        ):
            with self.assertRaises(ComponentWorkerError):
                validate_component_artifact_import(self.handoff, invalid)
        changed = replace(self.handoff, routing_identity=identity("new-routing"))
        with self.assertRaises(ComponentWorkerError):
            validate_component_artifact_import(changed, receipt)

    def test_empty_handoff_requires_no_unrelated_store(self):
        handoff = self._handoff({}, "pricing")
        receipt = import_component_artifacts(
            handoff,
            worker_identity=handoff.consumer.worker_identity,
            sources={},
            destination=self.destination,
        )
        validate_component_artifact_import(handoff, receipt)
        self.assertEqual(receipt.export_identities, ())

    def _distributed_lifecycle(
        self, *, corrupt_worker=False, kind=DependencyKind.BUILD
    ):
        lock = _diamond_lock(dependency_kind=kind)
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)
        by_name = {name: uri for uri, name in names.items()}
        catalog = ExecutionWorkerCatalog(
            (
                ExecutionWorker(
                    "command",
                    ExecutionWorkerKind.COMMAND,
                    target_profile=lock.target_name,
                    command=("dispatcher",),
                ),
                ExecutionWorker(
                    "ssh",
                    ExecutionWorkerKind.SSH,
                    target_profile=lock.target_name,
                    endpoint="worker@example.test",
                    workspace="/tmp/literate-ai-worker",
                ),
            )
        )
        routes = {
            uri: "ssh" if name == "reporting" else "command"
            for uri, name in names.items()
        }
        routing = plan_component_worker_routing(execution, lock, catalog, routes)
        handler = _TransportHandler(
            overlap=(by_name["pricing"], by_name["reporting"]),
            corrupt_worker=corrupt_worker,
        )
        dispatcher = RoutedComponentNodeDispatcher(
            catalog, command=handler, ssh=handler
        )
        ports = LifecyclePorts(execution, names)
        arguments = dict(
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=nodes,
            max_parallelism=2,
            component_worker_routing=routing,
            worker_catalog=catalog,
            component_node_dispatcher=dispatcher,
        )
        return execution, names, routing, handler, ports, arguments

    def test_standard_scheduler_overlaps_two_transports_and_imports_exact_products(
        self,
    ):
        for kind in (DependencyKind.BUILD, DependencyKind.TOOLCHAIN):
            with self.subTest(kind=kind):
                execution, names, _, handler, ports, arguments = (
                    self._distributed_lifecycle(kind=kind)
                )
                result = _service(ports).execute(execution, **arguments)

                self.assertTrue(result.successful)
                by_name = {name: uri for uri, name in names.items()}
                invoice = handler.outcomes[by_name["invoice-cli"]]
                expected = tuple(
                    sorted(
                        (
                            ports.realized_exports[by_name["pricing"]][0].identity,
                            ports.realized_exports[by_name["reporting"]][0].identity,
                        ),
                        key=lambda item: item.uri,
                    )
                )
                self.assertEqual(invoice.import_receipt.export_identities, expected)
                self.assertIn(by_name["pricing"], handler.started)
                self.assertIn(by_name["reporting"], handler.started)

    def test_wrong_worker_result_blocks_downstream_dispatch(self):
        execution, names, _, handler, ports, arguments = self._distributed_lifecycle(
            corrupt_worker=True
        )
        result = _service(ports).execute(execution, **arguments)

        self.assertFalse(result.successful)
        root_uri = next(uri for uri, name in names.items() if name == "invoice-cli")
        self.assertNotIn(root_uri, handler.started)

    def test_exact_current_recovery_reuses_results_and_changed_route_fails(self):
        execution, names, routing, handler, ports, arguments = (
            self._distributed_lifecycle()
        )
        first = _service(ports).execute(execution, **arguments)
        self.assertTrue(first.successful)
        recovery = {
            uri: ComponentNodeRecoveryCandidate(
                routing.identity,
                outcome.request_identity,
                outcome.worker_identity,
                outcome.result_identity,
                outcome.import_receipt,
                outcome.result,
            )
            for uri, outcome in handler.outcomes.items()
        }

        second_handler = _TransportHandler()
        second_dispatcher = RoutedComponentNodeDispatcher(
            arguments["worker_catalog"],
            command=second_handler,
            ssh=second_handler,
        )
        second_ports = LifecyclePorts(execution, names)
        second = _service(second_ports).execute(
            execution,
            **{
                **arguments,
                "component_node_dispatcher": second_dispatcher,
                "component_node_recovery": recovery,
            },
        )
        self.assertTrue(second.successful)
        self.assertEqual(second_handler.started, [])

        changed_assignments = list(routing.assignments)
        changed_assignments[0] = replace(
            changed_assignments[0],
            worker_id=(
                "ssh" if changed_assignments[0].worker_id == "command" else "command"
            ),
            worker_identity=(
                arguments["worker_catalog"]
                .worker(
                    "ssh"
                    if changed_assignments[0].worker_id == "command"
                    else "command"
                )
                .identity
            ),
        )
        changed = replace(
            routing,
            assignments=tuple(
                sorted(changed_assignments, key=lambda x: x.component_revision.uri)
            ),
        )
        stale = _service(LifecyclePorts(execution, names)).execute(
            execution,
            **{
                **arguments,
                "component_worker_routing": changed,
                "component_node_recovery": recovery,
            },
        )
        self.assertFalse(stale.successful)

    def test_pre_cancelled_worker_run_actuates_no_transport(self):
        execution, _, _, handler, ports, arguments = self._distributed_lifecycle()
        cancellation = threading.Event()
        cancellation.set()
        result = _service(ports).execute(
            execution, **arguments, cancellation=cancellation
        )

        self.assertFalse(result.successful)
        self.assertEqual(handler.started, [])
        self.assertTrue(handler.cancelled)


if __name__ == "__main__":
    unittest.main()
