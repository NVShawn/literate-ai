"""Production source and lifecycle phases share canonical action readiness."""

import unittest
from dataclasses import replace
from unittest.mock import Mock

from literate_ai.application.action_dag_planning import plan_lifecycle_action_dag
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts.capabilities import DependencyKind
from tests.unit.test_standard_project_lifecycle import (
    ContractEvidenceLifecyclePorts,
    LifecyclePorts,
    _decision,
    _diamond_lock,
    _identity,
    _names,
    _prepared_execution,
    _prepared_nodes,
    _service,
)


class StandardActionReadyQueueTests(unittest.TestCase):
    def test_build_receives_accepted_provider_evidence_before_reservation(self):
        lock = _diamond_lock(dependency_kind=DependencyKind.BUILD)
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = ContractEvidenceLifecyclePorts(execution, names)
        observed = {}

        class Builder:
            def retain_build_provider_evidence(self, plan, receipts):
                exports = tuple(
                    sorted(
                        (
                            export.identity
                            for receipt in receipts
                            for export in receipt.build.exports
                        ),
                        key=lambda item: item.uri,
                    )
                )
                self_test.assertTrue(
                    set(plan.provider_artifact_identities) <= set(exports)
                )
                from literate_ai.application.standard_provider_receipts import (
                    select_build_provider_receipts,
                )

                direct = tuple(
                    sorted(
                        (
                            export
                            for receipt in receipts
                            for export in receipt.build.exports
                            if export.identity in plan.provider_artifact_identities
                        ),
                        key=lambda item: item.identity.uri,
                    )
                )
                self_test.assertEqual(
                    select_build_provider_receipts(direct, receipts), receipts
                )
                observed[plan.identity] = receipts

            def build(self, plan, providers):
                raise AssertionError("reservation bypassed")

            def try_reserve_build(self, plan, providers):
                self_test.assertIn(plan.identity, observed)

                class Reservation:
                    def run(self):
                        return ports.build(plan, providers)

                    def release(self):
                        pass

                return Reservation()

        self_test = self
        service = _service(ports)
        service.builder = Builder()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertTrue(result.successful, str(result.node_results))
        self.assertEqual(len(observed), len(names))
        self.assertTrue(any(observed.values()))
        root_receipts = next(
            receipts
            for receipts in observed.values()
            if len(receipts) == len(names) - 1
        )
        self.assertEqual(
            {item.component_revision.uri for item in root_receipts},
            set(names) - {execution.root_revision.uri},
        )

    def test_exhausted_build_capacity_leaves_other_phases_runnable(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        waits = []
        released = []

        class Reservation:
            def __init__(self, plan, providers):
                self.plan, self.providers = plan, providers

            def run(self):
                return ports.build(self.plan, self.providers)

            def release(self):
                released.append(self.plan.component_revision)

        class Builder:
            def build(self, plan, providers):
                raise AssertionError("scheduler bypassed BUILD admission")

            def try_reserve_build(self, plan, providers):
                if (
                    names[plan.component_revision.uri] != "money"
                    and ("accept", "money") not in ports.events
                ):
                    waits.append(plan.component_revision)
                    return None
                return Reservation(plan, providers)

        service = _service(ports)
        service.builder = Builder()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(waits, "fixture did not exhaust BUILD capacity")
        self.assertEqual(len(released), len(names))
        self.assertEqual(len(set(released)), len(released))

    def test_invalid_reserved_build_releases_slot_and_cannot_reach_test(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        released = []

        class Reservation:
            def run(self):
                return object()

            def release(self):
                released.append(True)

        class Builder:
            def build(self, plan, providers):
                raise AssertionError("scheduler bypassed BUILD admission")

            def try_reserve_build(self, plan, providers):
                return Reservation()

        service = _service(ports)
        service.builder = Builder()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        self.assertTrue(released)
        self.assertFalse(any(event[0] == "test" for event in ports.events))
        self.assertTrue(
            any(
                node.failure_evidence.phase.value == "build"
                for node in result.node_results
                if node.failure_evidence is not None
            )
        )

    def test_exhausted_test_capacity_leaves_other_phases_runnable(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        waits = []
        released = []

        class Reservation:
            def __init__(self, plan, providers):
                self.plan, self.providers = plan, providers

            def run(self):
                return ports.test(self.plan, self.providers)

            def release(self):
                released.append(self.plan.component_revision)

        class Tester:
            def test(self, plan, providers):
                raise AssertionError("scheduler bypassed TEST admission")

            def try_reserve_test(self, plan, providers):
                if (
                    names[plan.component_revision.uri] != "money"
                    and ("accept", "money") not in ports.events
                ):
                    waits.append(plan.component_revision)
                    return None
                return Reservation(plan, providers)

        service = _service(ports)
        service.tester = Tester()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(waits, "fixture did not exhaust TEST capacity")
        self.assertEqual(len(released), len(names))
        self.assertEqual(len(set(released)), len(released))

    def test_invalid_reserved_test_releases_slot_and_cannot_reach_execution(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        released = []

        class Reservation:
            def run(self):
                return object()

            def release(self):
                released.append(True)

        class Tester:
            def test(self, plan, providers):
                raise AssertionError("scheduler bypassed TEST admission")

            def try_reserve_test(self, plan, providers):
                return Reservation()

        service = _service(ports)
        service.tester = Tester()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        self.assertTrue(released)
        self.assertFalse(any(event[0] == "execute" for event in ports.events))
        self.assertTrue(
            any(
                node.failure_evidence.phase.value == "test"
                for node in result.node_results
                if node.failure_evidence is not None
            )
        )

    def test_exhausted_execute_capacity_leaves_other_phases_runnable(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        waits = []
        released = []

        class Reservation:
            def __init__(self, plan, providers):
                self.plan, self.providers = plan, providers

            def run(self):
                return ports.execute(self.plan, self.providers)

            def release(self):
                released.append(self.plan.component_revision)

        class Executor:
            def execute(self, plan, providers):
                raise AssertionError("scheduler bypassed EXECUTE admission")

            def try_reserve_execute(self, plan, providers, scope, runtime_providers):
                if (
                    names[plan.component_revision.uri] != "money"
                    and ("accept", "money") not in ports.events
                ):
                    waits.append(plan.component_revision)
                    return None
                return Reservation(plan, providers)

        service = _service(ports)
        service.executor = Executor()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(waits, "fixture did not exhaust EXECUTE capacity")
        self.assertEqual(len(released), len(names))
        self.assertEqual(len(set(released)), len(released))

    def test_invalid_reserved_execution_releases_slot_and_cannot_reach_acceptance(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        released = []

        class Reservation:
            def run(self):
                return object()

            def release(self):
                released.append(True)

        class Executor:
            def execute(self, plan, providers):
                raise AssertionError("scheduler bypassed EXECUTE admission")

            def try_reserve_execute(self, plan, providers, scope, runtime_providers):
                return Reservation()

        service = _service(ports)
        service.executor = Executor()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        self.assertTrue(released)
        self.assertFalse(any(event[0] == "accept" for event in ports.events))
        self.assertTrue(
            any(
                node.failure_evidence.phase.value == "execute"
                for node in result.node_results
                if node.failure_evidence is not None
            )
        )

    def test_reserved_execution_cannot_bypass_current_runtime_scope_admission(self):
        from tests.unit.test_standard_runtime_scheduling import ScopedRuntimePorts

        for substitute in (False, True):
            with self.subTest(substitute=substitute):
                lock = _diamond_lock(dependency_kind=DependencyKind.RUNTIME)
                execution, requests = _prepared_execution(lock)
                names = _names(lock)
                ports = ScopedRuntimePorts(execution, names)
                released = []
                observed = []

                class Reservation:
                    def __init__(self, plan, exports, scope, providers):
                        self.arguments = plan, exports, scope, providers

                    def run(
                        self, observed=observed, substitute=substitute, ports=ports
                    ):
                        plan, exports, scope, providers = self.arguments
                        observed.append(scope)
                        if substitute:
                            scope = replace(
                                scope, execution_plan_identity=_identity("other-plan")
                            )
                        return ports.execute_scoped(plan, exports, scope, providers)

                    def release(self, released=released):
                        released.append(self.arguments[0].component_revision)

                class Executor:
                    def execute(self, *args):
                        raise AssertionError("unscoped execution")

                    def execute_scoped(self, *args):
                        raise AssertionError("scheduler bypassed EXECUTE reservation")

                    def try_reserve_execute(self, plan, exports, scope, providers):
                        return Reservation(plan, exports, scope, providers)

                service = _service(ports)
                service.executor = Executor()
                result = service.execute(
                    execution,
                    component_lock=lock,
                    invalidation=_decision(
                        execution, names, "money", tuple(names.values())
                    ),
                    prepared_nodes=_prepared_nodes(execution, requests),
                    max_parallelism=2,
                )
                self.assertEqual(result.successful, not substitute)
                self.assertTrue(released)
                self.assertEqual(len(released), len(observed))
                self.assertTrue(
                    all(
                        scope.execution_plan_identity == execution.identity
                        for scope in observed
                    )
                )
                if substitute:
                    self.assertFalse(
                        any(event[0] == "accept" for event in ports.events)
                    )
                    self.assertTrue(
                        any(
                            node.failure_evidence is not None
                            and node.failure_evidence.phase.value == "execute"
                            for node in result.node_results
                        )
                    )
                else:
                    self.assertTrue(
                        any(scope.runtime_dependencies for scope in observed)
                    )

    def test_generation_and_post_source_steps_share_canonical_ready_order(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        result = _service(ports).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=1,
        )
        self.assertTrue(result.successful)
        labels = {
            LifecycleActionKind.GENERATE: "generate",
            LifecycleActionKind.INDEX: "index",
            LifecycleActionKind.BUILD_INTENT: "intent",
            LifecycleActionKind.AUTHORIZE: "authorize",
            LifecycleActionKind.PLAN: "finalize",
            LifecycleActionKind.BUILD: "build",
            LifecycleActionKind.TEST: "test",
            LifecycleActionKind.EXECUTE: "execute",
            LifecycleActionKind.ACCEPT: "accept",
        }
        actions = {
            (labels[node.kind], names[node.component_revision.uri]): node
            for node in plan_lifecycle_action_dag(execution, worker_ids=("local",))
            if node.kind in labels
        }
        remaining = dict(actions)
        accepted = set()
        for event in ports.events:
            if event not in actions:
                continue
            ready = {
                key: node
                for key, node in remaining.items()
                if set(node.predecessor_ids) <= accepted
            }
            self.assertTrue(ready, f"{event} ran before its prerequisites")
            expected = min(ready, key=lambda key: ready[key].identity.uri)
            self.assertEqual(
                event,
                expected,
                "generation and post-source work must compete in one action queue",
            )
            accepted.add(remaining.pop(event).action_id)
        self.assertFalse(remaining)

    def test_exhausted_index_capacity_leaves_other_phases_runnable(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        waits = []
        released = []

        class Reservation:
            def __init__(self, component, source):
                self.component, self.source = component, source
                self.done = False

            def run(self):
                return ports.index(self.component, self.source)

            def release(self):
                if not self.done:
                    released.append(self.component)
                    self.done = True

        class Indexer:
            def index(self, component, source):
                raise AssertionError("scheduler bypassed worker admission")

            def try_reserve_index(self, component, source):
                if (
                    names[component.uri] != "money"
                    and ("accept", "money") not in ports.events
                ):
                    waits.append(component)
                    return None
                return Reservation(component, source)

        service = _service(ports)
        service.indexer = Indexer()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        self.assertTrue(waits, "fixture did not exhaust remote capacity")
        self.assertEqual(len(released), len(names))

    def test_reserved_slot_releases_when_guard_refuses_execution(self):
        from literate_ai.application.standard_project_lifecycle import (
            StandardProjectLifecycleService,
            _LifecycleStep,
        )
        from literate_ai.contracts.standard_lifecycle_checkpoint import (
            StandardLifecycleStage,
        )

        reservation = Mock()
        step = _LifecycleStep(
            StandardLifecycleStage.SOURCE_INDEX,
            Mock(),
            guard=Mock(side_effect=RuntimeError("custody changed")),
        )
        with self.assertRaisesRegex(RuntimeError, "custody changed"):
            StandardProjectLifecycleService._run_reserved_step(None, step, reservation)
        reservation.run.assert_not_called()
        reservation.release.assert_called_once_with()

    def test_admission_failure_is_retained_as_source_index_failure(self):
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        service = _service(ports)

        class Indexer:
            def __init__(self):
                self.index = Mock()
                self.try_reserve_index = Mock(
                    side_effect=RuntimeError("admission expired")
                )

        service.indexer = Indexer()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        service.indexer.index.assert_not_called()
        self.assertTrue(service.indexer.try_reserve_index.called)
        for node in result.node_results:
            self.assertEqual(node.failure_evidence.phase.value, "source-index")

    def test_plan_reservations_use_the_queue_and_still_validate_returned_plans(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                lock = _diamond_lock()
                execution, requests = _prepared_execution(lock)
                names = _names(lock)
                ports = LifecyclePorts(execution, names)
                released = []

                class Reservation:
                    def __init__(self, intent, authorization):
                        self.intent, self.authorization = intent, authorization

                    def run(self, invalid=invalid, ports=ports):
                        return (
                            object()
                            if invalid
                            else ports.finalize(self.intent, self.authorization)
                        )

                    def release(self, released=released):
                        released.append(self.intent.identity)

                class Finalizer:
                    def finalize(self, intent, authorization):
                        raise AssertionError("queue bypassed phase admission")

                    def try_reserve_plan(self, intent, authorization):
                        return Reservation(intent, authorization)

                service = _service(ports)
                service.build_plan_finalizer = Finalizer()
                result = service.execute(
                    execution,
                    component_lock=lock,
                    invalidation=_decision(
                        execution, names, "money", tuple(names.values())
                    ),
                    prepared_nodes=_prepared_nodes(execution, requests),
                    max_parallelism=2,
                )
                self.assertEqual(result.successful, not invalid)
                self.assertTrue(released)
                if invalid:
                    self.assertFalse(any(event[0] == "build" for event in ports.events))
                    self.assertTrue(
                        any(
                            node.failure_evidence.phase.value == "build-plan"
                            for node in result.node_results
                            if node.failure_evidence is not None
                        )
                    )
                else:
                    self.assertEqual(len(released), len(names))

    def test_intent_reservations_receive_completed_index_and_validate_results(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                lock = _diamond_lock()
                execution, requests = _prepared_execution(lock)
                names = _names(lock)
                ports = LifecyclePorts(execution, names)
                released = []
                observed = []

                class Reservation:
                    def __init__(self, arguments):
                        self.arguments = arguments

                    def run(self, invalid=invalid, ports=ports):
                        return object() if invalid else ports.create(*self.arguments)

                    def release(self, released=released):
                        released.append(self.arguments[1].component_revision)

                class Dispatcher:
                    def try_reserve_intent(
                        self,
                        plan,
                        generation,
                        candidate,
                        index,
                        providers,
                        packages,
                        receipts,
                        ports=ports,
                        observed=observed,
                        names=names,
                    ):
                        name = names[generation.component_revision.uri]
                        observed.append((name, index, receipts))
                        if ("index", name) not in ports.events:
                            raise AssertionError("intent ran before index completion")
                        return Reservation(
                            (plan, generation, candidate, providers, packages)
                        )

                service = _service(ports)
                service.build_intent_dispatcher = Dispatcher()
                service.build_intent_factory = Mock(
                    create=Mock(side_effect=AssertionError("local intent fallback"))
                )
                result = service.execute(
                    execution,
                    component_lock=lock,
                    invalidation=_decision(
                        execution, names, "money", tuple(names.values())
                    ),
                    prepared_nodes=_prepared_nodes(execution, requests),
                    max_parallelism=2,
                )
                self.assertEqual(result.successful, not invalid)
                self.assertTrue(observed)
                self.assertEqual(len(released), len(observed))
                service.build_intent_factory.create.assert_not_called()
                for name, index, receipts in observed:
                    self.assertEqual(index, _identity(f"index-{name}"))
                    self.assertEqual(receipts, ())
                if invalid:
                    self.assertFalse(any(event[0] == "build" for event in ports.events))
                else:
                    self.assertEqual(len(released), len(names))

    def test_unattested_build_provider_blocks_remote_intent(self):
        from literate_ai.contracts.capabilities import DependencyKind

        lock = _diamond_lock(dependency_kind=DependencyKind.BUILD)
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        dispatched = []

        class Reservation:
            def __init__(self, arguments):
                self.arguments = arguments

            def run(self):
                return ports.create(*self.arguments)

            def release(self):
                pass

        class Dispatcher:
            def try_reserve_intent(
                self, plan, generation, candidate, index, providers, packages, receipts
            ):
                dispatched.append(names[generation.component_revision.uri])
                return Reservation((plan, generation, candidate, providers, packages))

        service = _service(ports)
        service.build_intent_dispatcher = Dispatcher()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        self.assertEqual(dispatched, ["money"])
        self.assertIn(("accept", "money"), ports.events)
        self.assertFalse(any(event == ("intent", "pricing") for event in ports.events))
        self.assertTrue(
            any(
                node.failure_evidence is not None
                and "exact accepted provider evidence" in str(node.failure_evidence)
                for node in result.node_results
            )
        )

    def test_build_provider_receipts_are_captured_after_acceptance(self):
        from literate_ai.contracts.capabilities import DependencyKind
        from tests.unit.test_standard_project_lifecycle import (
            ContractEvidenceLifecyclePorts,
        )

        lock = _diamond_lock(dependency_kind=DependencyKind.BUILD)
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = ContractEvidenceLifecyclePorts(execution, names)
        captured = []

        class Reservation:
            def __init__(self, arguments):
                self.arguments = arguments

            def run(self):
                return ports.create(*self.arguments)

            def release(self):
                pass

        class Dispatcher:
            def try_reserve_intent(
                self, plan, generation, candidate, index, providers, packages, receipts
            ):
                for receipt in receipts:
                    if (
                        "accept",
                        names[receipt.component_revision.uri],
                    ) not in ports.events:
                        raise AssertionError(
                            "provider receipt captured before acceptance"
                        )
                    if (
                        receipt
                        != ports.typed_acceptances[receipt.component_revision.uri]
                    ):
                        raise AssertionError("provider receipt was substituted")
                if set(providers) != {
                    export for receipt in receipts for export in receipt.build.exports
                }:
                    raise AssertionError("receipt exports differ from intent inputs")
                captured.append(receipts)
                return Reservation((plan, generation, candidate, providers, packages))

        service = _service(ports)
        service.build_intent_dispatcher = Dispatcher()
        result = service.execute(
            execution,
            component_lock=lock,
            invalidation=_decision(execution, names, "money", tuple(names.values())),
            prepared_nodes=_prepared_nodes(execution, requests),
            max_parallelism=2,
        )
        self.assertTrue(result.successful, str(result.node_results))
        self.assertEqual(len(captured), len(names))
        self.assertTrue(any(captured))

    def test_authorization_reservations_preserve_queue_result_validation(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                lock = _diamond_lock()
                execution, requests = _prepared_execution(lock)
                names = _names(lock)
                ports = LifecyclePorts(execution, names)
                released = []

                class Reservation:
                    def __init__(self, intent, index):
                        self.intent, self.index = intent, index

                    def run(self, invalid=invalid, ports=ports):
                        return (
                            object()
                            if invalid
                            else ports.authorize(self.intent, self.index)
                        )

                    def release(self, released=released):
                        released.append(self.intent.identity)

                class Authorizer:
                    def authorize(self, intent, index):
                        raise AssertionError("queue bypassed authorization admission")

                    def try_reserve_authorization(self, intent, index):
                        return Reservation(intent, index)

                service = _service(ports)
                service.authorizer = Authorizer()
                result = service.execute(
                    execution,
                    component_lock=lock,
                    invalidation=_decision(
                        execution, names, "money", tuple(names.values())
                    ),
                    prepared_nodes=_prepared_nodes(execution, requests),
                    max_parallelism=2,
                )
                self.assertEqual(result.successful, not invalid)
                self.assertTrue(released)
                if invalid:
                    self.assertFalse(
                        any(event[0] in ("finalize", "build") for event in ports.events)
                    )
                else:
                    self.assertEqual(len(released), len(names))
