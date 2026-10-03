"""Runtime-only edges gate execution while allowing independent compilation."""

import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.contracts import StandardExecutionAuthority
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.standard_execution_inputs import standard_execution_request
from literate_ai.security import BuildAuthorization, SecurityProfile
from tests.unit.test_component_execution_planning import _diamond_lock
from tests.unit.test_component_generation_scheduling import _names, _prepared_execution
from tests.unit.test_standard_project_lifecycle import (
    ContractEvidenceLifecyclePorts,
    _accepted,
    _decision,
    _identity,
    _prepared_nodes,
    _service,
    canonical_identity,
)


class ScopedRuntimePorts(ContractEvidenceLifecyclePorts):
    """Typed controller fixture; real process/grant custody has adapter tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution_scopes = {}
        self.execution_inputs = {}

    def execute_scoped(self, plan, exports, scope, providers):
        self.execution_scopes[plan.component_revision.uri] = scope
        self.execution_inputs[plan.component_revision.uri] = providers
        original = super().execute(plan, exports)
        command, runtime = _identity("scoped-command"), _identity("scoped-runtime")
        request = standard_execution_request(
            scope, plan.request.source_tree_identity, command, runtime
        )
        now = datetime(2026, 8, 7, tzinfo=UTC)
        grant = BuildAuthorization(
            "fixture-scoped",
            scope.identity.uri,
            canonical_identity(request.to_dict()).uri,
            plan.component_revision.uri,
            "fixture",
            "execute admitted runtime inputs",
            SecurityProfile.CONSTRAINED,
            request.requested_privileges,
            now,
            now + timedelta(minutes=30),
        )
        authority = StandardExecutionAuthority(
            scope, plan.request.source_tree_identity, command, runtime, grant
        )
        result = replace(
            original,
            provider_artifact_identities=scope.provider_artifact_identities,
            execution_authority=authority,
        )
        self.typed_acceptances[plan.component_revision.uri] = replace(
            self.typed_acceptances[plan.component_revision.uri], execution=result
        )
        return result


class StandardRuntimeSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.lock = _diamond_lock(dependency_kind=DependencyKind.RUNTIME)
        self.execution, requests = _prepared_execution(self.lock)
        self.names = _names(self.lock)
        self.nodes = _prepared_nodes(self.execution, requests)

    def run_lifecycle(self, ports, **kwargs):
        return _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                () if kwargs.get("resume_candidates") else tuple(self.names.values()),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
            **kwargs,
        )

    def test_full_runtime_receipts_arrive_before_execution_reservation(self):
        for kind in (
            DependencyKind.RUNTIME,
            DependencyKind.BUILD,
            DependencyKind.TOOLCHAIN,
        ):
            with self.subTest(kind=kind):
                lock = _diamond_lock(dependency_kind=kind)
                execution, requests = _prepared_execution(lock)
                names = _names(lock)
                retained, reserved, released = {}, [], []

                class Ports(ScopedRuntimePorts):
                    def retain_execution_provider_evidence(
                        self, plan, scope, receipts, retained=retained
                    ):
                        previous = retained.setdefault(scope.identity, receipts)
                        if previous != receipts:
                            raise AssertionError("runtime receipts changed")
                        if {
                            item.identity
                            for receipt in receipts
                            for item in receipt.build.exports
                        } != set(scope.provider_artifact_identities):
                            raise AssertionError("runtime provider closure differs")

                    def try_reserve_execute(
                        self,
                        plan,
                        exports,
                        scope,
                        providers,
                        retained=retained,
                        reserved=reserved,
                        released=released,
                    ):
                        if scope.identity not in retained:
                            raise AssertionError("reserved without runtime receipts")
                        reserved.append(scope)
                        ports = self

                        class Reservation:
                            def run(self):
                                return ports.execute_scoped(
                                    plan, exports, scope, providers
                                )

                            def release(self, released=released):
                                released.append(scope.identity)

                        return Reservation()

                    def execute_scoped(
                        self, plan, exports, scope, providers, retained=retained
                    ):
                        if scope.identity not in retained:
                            raise AssertionError("executed without runtime receipts")
                        return super().execute_scoped(plan, exports, scope, providers)

                ports = Ports(execution, names)
                result = _service(ports).execute(
                    execution,
                    component_lock=lock,
                    invalidation=_decision(
                        execution, names, "money", tuple(names.values())
                    ),
                    prepared_nodes=_prepared_nodes(execution, requests),
                    max_parallelism=2,
                )
                self.assertTrue(result.successful)
                self.assertEqual(len(reserved), len(names))
                self.assertCountEqual(released, [scope.identity for scope in reserved])
                root = next(
                    scope
                    for scope in reserved
                    if scope.component_revision == execution.root_revision
                )
                self.assertEqual(
                    {
                        names[receipt.component_revision.uri]
                        for receipt in retained[root.identity]
                    },
                    {"money", "pricing", "reporting"},
                )
                if kind is DependencyKind.RUNTIME:
                    self.assertEqual(root.build_provider_artifact_identities, ())
                    self.assertTrue(root.runtime_dependencies)

    def test_runtime_receipt_retention_failure_prevents_execution_and_acceptance(self):
        class Ports(ScopedRuntimePorts):
            def retain_execution_provider_evidence(self, plan, scope, receipts):
                raise RuntimeError("runtime receipts unavailable")

            def try_reserve_execute(self, *args):
                raise AssertionError("reserved after receipt refusal")

        ports = Ports(self.execution, self.names)
        result = self.run_lifecycle(ports)
        self.assertFalse(result.successful)
        self.assertFalse(
            any(event[0] in {"execute", "accept"} for event in ports.events)
        )
        self.assertTrue(
            any(
                node.failure_evidence is not None
                and node.failure_evidence.phase.value == "execute"
                for node in result.node_results
            )
        )

    def test_runtime_provider_waits_for_execution_not_compilation(self):
        consumer_built = threading.Event()
        provider_started = threading.Event()

        class OverlapPorts(ScopedRuntimePorts):
            overlapped = False

            def build(inner, plan, providers):
                name = inner.names[plan.component_revision.uri]
                if name == "money":
                    provider_started.set()
                    inner.overlapped = consumer_built.wait(5)
                elif name == "invoice-cli":
                    if not provider_started.wait(5):
                        raise AssertionError("runtime provider never started")
                output = super().build(plan, providers)
                if name == "invoice-cli":
                    consumer_built.set()
                return output

        ports = OverlapPorts(self.execution, self.names)
        result = self.run_lifecycle(ports)
        self.assertTrue(result.successful)
        self.assertTrue(ports.overlapped)
        self.assertLess(
            ports.events.index(("build", "invoice-cli")),
            ports.events.index(("accept", "money")),
        )
        invoice = next(uri for uri, name in self.names.items() if name == "invoice-cli")
        self.assertEqual(ports.plans[invoice].provider_artifact_identities, ())
        scope = ports.execution_scopes[invoice]
        # The diamond's indirect money provider must reach the root process too.
        self.assertEqual(
            {
                self.names[item.component_revision.uri]
                for item in ports.execution_inputs[invoice]
            },
            {"money", "pricing", "reporting"},
        )
        self.assertEqual(
            set(scope.provider_artifact_identities),
            {item.identity for item in ports.execution_inputs[invoice]},
        )
        for name in ("money", "pricing", "reporting"):
            self.assertLess(
                ports.events.index(("accept", name)),
                ports.events.index(("execute", "invoice-cli")),
            )

    def test_build_and_toolchain_execution_include_transitive_providers(self):
        for kind in (DependencyKind.BUILD, DependencyKind.TOOLCHAIN):
            with self.subTest(kind=kind):
                lock = _diamond_lock(dependency_kind=kind)
                execution, requests = _prepared_execution(lock)
                names = _names(lock)
                ports = ScopedRuntimePorts(execution, names)
                result = _service(ports).execute(
                    execution,
                    component_lock=lock,
                    invalidation=_decision(
                        execution, names, "money", tuple(names.values())
                    ),
                    prepared_nodes=_prepared_nodes(execution, requests),
                    max_parallelism=2,
                )
                self.assertTrue(result.successful)
                invoice = next(
                    uri for uri, name in names.items() if name == "invoice-cli"
                )
                money = next(uri for uri, name in names.items() if name == "money")
                scope = ports.execution_scopes[invoice]
                self.assertEqual(scope.runtime_dependencies, ())
                self.assertEqual(
                    set(scope.transitive_provider_artifact_identities),
                    {item.identity for item in ports.realized_exports[money]},
                )
                self.assertEqual(
                    {
                        names[item.component_revision.uri]
                        for item in ports.execution_inputs[invoice]
                    },
                    {"money", "pricing", "reporting"},
                )

    def test_failed_runtime_provider_preserves_build_but_prevents_execution(self):
        class FailedPorts(ScopedRuntimePorts):
            def build(inner, plan, providers):
                if inner.names[plan.component_revision.uri] == "money":
                    raise RuntimeError("runtime provider rejected")
                return super().build(plan, providers)

        ports = FailedPorts(self.execution, self.names)
        result = self.run_lifecycle(ports)
        self.assertFalse(result.successful)
        self.assertIn(("build", "invoice-cli"), ports.events)
        self.assertNotIn(("execute", "invoice-cli"), ports.events)
        self.assertFalse(ports.issued_receipts)
        self.assertFalse(ports.published_memberships)

    def test_substituted_scope_cannot_publish(self):
        class OmittedPorts(ScopedRuntimePorts):
            def execute_scoped(inner, *args):
                return replace(super().execute_scoped(*args), execution_authority=None)

        ports = OmittedPorts(self.execution, self.names)
        result = self.run_lifecycle(ports)
        self.assertFalse(result.successful)
        self.assertIn(
            "standard_lifecycle.execution_scope_mismatch",
            {item.failure_code for item in result.node_results},
        )
        self.assertFalse(ports.issued_receipts)

    def test_runtime_change_retains_compilation_and_revalidates_opaque_resume(self):
        baseline = ScopedRuntimePorts(self.execution, self.names)
        first = self.run_lifecycle(baseline)
        self.assertTrue(first.successful)
        by_uri = {item.component_revision.uri: item for item in first.node_results}
        resume = {
            uri: _accepted(
                baseline.plans[uri],
                self.nodes[uri],
                name,
                baseline.realized_exports[uri],
                by_uri[uri].source_output,
            )
            for uri, name in self.names.items()
        }
        fresh = ScopedRuntimePorts(self.execution, self.names)
        fresh.generated_outputs = {
            uri: node.source_output for uri, node in by_uri.items()
        }
        fresh.changed_exports = {"money"}
        fresh.export_label = "rotated-runtime"
        second = self.run_lifecycle(fresh, resume_candidates=resume)
        self.assertTrue(second.successful)
        invoice = next(uri for uri, name in self.names.items() if name == "invoice-cli")
        self.assertEqual(
            baseline.plans[invoice].identity, fresh.plans[invoice].identity
        )
        self.assertEqual(
            baseline.realized_exports[invoice], fresh.realized_exports[invoice]
        )
        self.assertNotEqual(
            baseline.execution_scopes[invoice].identity,
            fresh.execution_scopes[invoice].identity,
        )
        self.assertIn(("execute", "invoice-cli"), fresh.events)
