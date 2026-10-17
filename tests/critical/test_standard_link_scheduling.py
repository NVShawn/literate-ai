"""LINK joins the Component ready queue without withholding accepted providers."""

import threading
import unittest

import tests.support.fixtures_test_standard_project_lifecycle as fixture
from literate_ai.application.artifact_graph import realize_manifest
from literate_ai.application.standard_project_lifecycle import (
    StandardProjectLifecycleError,
)
from literate_ai.contracts.capabilities import DependencyKind


class StandardLinkSchedulingTests(unittest.TestCase):
    def configure(self, kind=DependencyKind.PACKAGING, ports_type=None):
        lock = fixture._diamond_lock(dependency_kind=kind)
        execution, requests = fixture._prepared_execution(lock)
        names = fixture._names(lock)
        ports = (ports_type or fixture.ContractEvidenceLifecyclePorts)(execution, names)
        service = fixture._service(ports)
        arguments = dict(
            component_lock=lock,
            invalidation=fixture._decision(
                execution, names, "money", tuple(names.values())
            ),
            prepared_nodes=fixture._prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        return execution, names, ports, service, arguments

    def test_packaging_links_follow_provider_links_before_project_assembly(self):
        execution, names, ports, service, arguments = self.configure()
        completed = []

        class Linker:
            def link(self, plan, receipt):
                self_test.assertEqual(
                    receipt, ports.typed_acceptances[plan.component_revision.uri]
                )
                name = names[plan.component_revision.uri]
                providers = {
                    "pricing": ("money",),
                    "reporting": ("money",),
                    "invoice-cli": ("pricing", "reporting"),
                }.get(name, ())
                for provider in providers:
                    self_test.assertIn(provider, completed)
                completed.append(name)
                ports._record("link", name)
                return realize_manifest(plan.manifest, receipt.build.exports)

        self_test = self
        service.component_linker = Linker()
        result = service.execute(execution, **arguments)
        self.assertTrue(result.successful)
        self.assertCountEqual(completed, names.values())
        package_index = next(
            i for i, event in enumerate(ports.events) if event[0] == "create-package"
        )
        self.assertTrue(
            all(
                i < package_index
                for i, event in enumerate(ports.events)
                if event[0] == "link"
            )
        )

    def test_link_runs_while_an_unrelated_component_is_still_accepting(self):
        linked = threading.Event()

        class Ports(fixture.ContractEvidenceLifecyclePorts):
            def accept(self, plan, test_identity, execution_identity):
                if self.names[plan.component_revision.uri] == "pricing":
                    if not linked.wait(10):
                        raise AssertionError(
                            "LINK was held behind all-component acceptance"
                        )
                return super().accept(plan, test_identity, execution_identity)

        execution, names, ports, service, arguments = self.configure(
            DependencyKind.GENERATION, Ports
        )

        class Linker:
            def link(self, plan, receipt):
                if names[plan.component_revision.uri] == "money":
                    linked.set()
                return realize_manifest(plan.manifest, receipt.build.exports)

        service.component_linker = Linker()
        self.assertTrue(service.execute(execution, **arguments).successful)
        self.assertTrue(linked.is_set())

    def test_reserved_link_runs_instead_of_direct_operation(self):
        execution, names, ports, service, arguments = self.configure()
        calls = []
        attempts = {}

        class Reservation:
            def __init__(self, plan, receipt):
                self.plan, self.receipt = plan, receipt

            def run(self):
                calls.append(self.plan.component_revision)
                return realize_manifest(self.plan.manifest, self.receipt.build.exports)

            def release(self):
                raise AssertionError("completed reservation released by controller")

        class Linker:
            def try_reserve_link(self, plan, receipt):
                key = plan.component_revision
                attempts[key] = attempts.get(key, 0) + 1
                if attempts[key] == 1:
                    return None
                return Reservation(plan, receipt)

            def link(self, plan, receipt):
                raise AssertionError("reserved LINK must replace direct dispatch")

        service.component_linker = Linker()
        self.assertTrue(service.execute(execution, **arguments).successful)
        self.assertEqual(len(calls), len(names))
        self.assertTrue(all(count == 2 for count in attempts.values()))

    def test_wrong_manifest_refuses_before_packaging(self):
        execution, names, ports, service, arguments = self.configure()

        class Linker:
            def link(self, plan, receipt):
                return None

        service.component_linker = Linker()
        with self.assertRaises(StandardProjectLifecycleError) as error:
            service.execute(execution, **arguments)
        self.assertEqual(
            error.exception.code, "standard_lifecycle.link_manifest_mismatch"
        )
        self.assertFalse(any(event[0] == "create-package" for event in ports.events))

    def test_failed_provider_skips_dependent_links_and_returns_failure(self):
        class Ports(fixture.ContractEvidenceLifecyclePorts):
            def accept(self, plan, test_identity, execution_identity):
                if self.names[plan.component_revision.uri] == "money":
                    raise RuntimeError("provider acceptance failed")
                return super().accept(plan, test_identity, execution_identity)

        execution, names, ports, service, arguments = self.configure(ports_type=Ports)

        class Linker:
            def link(self, plan, receipt):
                raise AssertionError("all links depend on the failed provider")

        service.component_linker = Linker()
        result = service.execute(execution, **arguments)
        self.assertFalse(result.successful)
        self.assertFalse(any(event[0] == "create-package" for event in ports.events))

    def test_ready_links_share_canonical_order_with_other_component_stages(self):
        from literate_ai.application.action_dag_planning import (
            plan_lifecycle_action_dag,
        )
        from literate_ai.application.action_dag_scheduler import LifecycleActionKind

        accepted, linked, violations = set(), set(), []
        kinds = {
            "generate": LifecycleActionKind.GENERATE,
            "index": LifecycleActionKind.INDEX,
            "intent": LifecycleActionKind.BUILD_INTENT,
            "authorize": LifecycleActionKind.AUTHORIZE,
            "finalize": LifecycleActionKind.PLAN,
            "build": LifecycleActionKind.BUILD,
            "test": LifecycleActionKind.TEST,
            "execute": LifecycleActionKind.EXECUTE,
            "accept": LifecycleActionKind.ACCEPT,
        }

        class Ports(fixture.ContractEvidenceLifecyclePorts):
            def _record(self, stage, component="project"):
                if stage in kinds:
                    selected = order[component, kinds[stage]]
                    for name in accepted - linked:
                        if order[name, LifecycleActionKind.LINK] < selected:
                            violations.append((name, stage, component))
                super()._record(stage, component)

            def accept(self, plan, test_identity, execution_identity):
                result = super().accept(plan, test_identity, execution_identity)
                accepted.add(self.names[plan.component_revision.uri])
                return result

        execution, names, ports, service, arguments = self.configure(
            DependencyKind.GENERATION, Ports
        )
        order = {
            (names[node.component_revision.uri], node.kind): node.identity.uri
            for node in plan_lifecycle_action_dag(execution, worker_ids=("local",))
        }

        class Linker:
            def link(self, plan, receipt):
                linked.add(names[plan.component_revision.uri])
                return realize_manifest(plan.manifest, receipt.build.exports)

        service.component_linker = Linker()
        arguments["max_parallelism"] = 1
        self.assertTrue(service.execute(execution, **arguments).successful)
        self.assertEqual(linked, set(names.values()))
        self.assertEqual(violations, [], "ready LINK was bypassed by a later action")
