"""Exact routing, public scope, and real byte custody across worker stores."""

from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.component_artifact_custody import import_component_artifacts
from literate_ai.adapters.component_worker_dispatch import RoutedComponentNodeDispatcher
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_workers import (
    ComponentNodeDispatchOutcome,
    ComponentWorkerError,
    component_artifact_handoff,
    component_node_payload_result,
    plan_component_worker_routing,
    validate_component_artifact_import,
)
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
from literate_ai.storage.cas import BlobIntegrityError, FileSystemCAS
from tests.support.fixtures_test_component_execution_planning import (
    _diamond_lock,
    _models,
)
from tests.support.fixtures_test_component_lock_contracts import identity
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

    def _distributed_lifecycle(self, *, corrupt_worker=False):
        lock = _diamond_lock(dependency_kind=DependencyKind.BUILD)
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

    def test_wrong_worker_result_blocks_downstream_dispatch(self):
        execution, names, _, handler, ports, arguments = self._distributed_lifecycle(
            corrupt_worker=True
        )
        result = _service(ports).execute(execution, **arguments)

        self.assertFalse(result.successful)
        root_uri = next(uri for uri, name in names.items() if name == "invoice-cli")
        self.assertNotIn(root_uri, handler.started)


if __name__ == "__main__":
    unittest.main()
