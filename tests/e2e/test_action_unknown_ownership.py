"""Unknown remote ownership retains capacity/claims; shared failure stays correlated."""

from __future__ import annotations

import threading
import time
import unittest
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_slots import CommandActionSlots
from literate_ai.application.action_artifact_dispatch import (
    CoalescingArtifactDispatcher,
)
from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDagScheduler,
    LifecycleActionDispatchRequest,
    LifecycleActionDisposition,
    LifecycleActionTermination,
)
from literate_ai.application.action_dispatch_policy import (
    ActionDispatchPolicy,
    ActionResources,
)
from tests.e2e.test_action_dag_scheduler import (
    _identity,
    _node,
    _RecordingDispatcher,
    _worker,
)


class UnknownOwnershipTests(unittest.TestCase):
    def test_unknown_holds_worker_and_cross_worker_claim_until_trusted_resolution(self):
        lost = threading.Event()

        class Dispatcher(_RecordingDispatcher):
            def dispatch(inner, request):
                if request.action.action_id == "lost":
                    outcome = super().dispatch(request)
                    lost.set()
                    return outcome
                if request.action.action_id == "trigger":
                    if not lost.wait(2):
                        raise TimeoutError("fixture did not become unknown")
                return super().dispatch(request)

        dispatcher = Dispatcher({"lost": "worker-lost"})
        scheduler = LifecycleActionDagScheduler()
        policy = ActionDispatchPolicy(
            ActionResources(cpu_millis=1000), ("protected-authority",)
        )
        result = scheduler.run(
            (
                _node("lost"),
                _node("trigger", workers=("beta",)),
                _node("sibling", predecessors=("trigger",), workers=("beta",)),
                _node("healthy", predecessors=("trigger",), workers=("beta",)),
            ),
            (_worker("alpha"), _worker("beta")),
            dispatcher,
            deadline_identity=_identity("deadline"),
            action_policies={"lost": policy, "sibling": policy},
            worker_resources={
                "alpha": ActionResources(cpu_millis=1000),
                "beta": ActionResources(cpu_millis=1000),
            },
        )
        values = {value.action_id: value for value in result.results}
        self.assertEqual(values["lost"].disposition, LifecycleActionDisposition.UNKNOWN)
        self.assertEqual(
            values["sibling"].disposition, LifecycleActionDisposition.BLOCKED
        )
        self.assertEqual(
            values["healthy"].disposition, LifecycleActionDisposition.ACCEPTED
        )
        self.assertNotIn(
            "sibling", {request.action.action_id for request in dispatcher.requests}
        )
        self.assertEqual(len(result.unresolved_ownership), 1)
        pending = result.unresolved_ownership[0]
        self.assertEqual(pending.policy.exclusive_claims, ("protected-authority",))
        again = scheduler.run(
            (_node("later"),),
            (_worker("alpha"),),
            _RecordingDispatcher(),
            deadline_identity=_identity("deadline"),
        )
        self.assertEqual(
            again.results[0].disposition, LifecycleActionDisposition.BLOCKED
        )
        scheduler.resolve_ownership(
            LifecycleActionTermination(
                pending.request.identity,
                pending.request.worker.worker_identity,
                _identity("actual-terminal-record"),
                "owned-terminal",
                0,
            )
        )
        after = scheduler.run(
            (_node("later"),),
            (_worker("alpha"),),
            _RecordingDispatcher(),
            deadline_identity=_identity("deadline"),
        )
        self.assertEqual(
            after.results[0].disposition, LifecycleActionDisposition.ACCEPTED
        )
        self.assertEqual(scheduler.unresolved_ownership, ())

    def test_malformed_outcome_preserves_hold_across_subsequent_schedule(self):
        class Dispatcher:
            def dispatch(self, request):
                return object()

        scheduler = LifecycleActionDagScheduler()
        with self.assertRaisesRegex(ActionDagSchedulingError, "another request"):
            scheduler.run(
                (_node("malformed"),),
                (_worker("alpha"),),
                Dispatcher(),
                deadline_identity=_identity("deadline"),
            )
        self.assertEqual(len(scheduler.unresolved_ownership), 1)
        result = scheduler.run(
            (_node("later"),),
            (_worker("alpha"),),
            _RecordingDispatcher(),
            deadline_identity=_identity("deadline"),
        )
        self.assertEqual(
            result.results[0].disposition, LifecycleActionDisposition.BLOCKED
        )

    def test_production_slot_reservation_retains_unknown_actuation(self):
        worker = _worker("alpha", slots=2)
        deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=10))
        slots = CommandActionSlots((worker,), deadline)
        held = []

        def operation(selected, slot):
            current = LifecycleActionDispatchRequest(
                _identity("schedule"),
                _node("lost"),
                selected,
                slot,
                (),
                deadline.identity,
            )
            held.append(current)
            error = ActionWireError(
                "action_wire.expired", "owned request status is unknown"
            )
            error.unknown_ownership_request = current
            raise error

        reserved = slots.try_reserve(operation)
        with self.assertRaises(ActionWireError):
            reserved.run()
        self.assertEqual(slots.unresolved_ownership, tuple(held))
        self.assertIsNone(slots.try_reserve(lambda worker, slot: None))
        with self.assertRaisesRegex(ActionWireError, "unresolved"):
            with slots.acquire():
                pass
        slots.resolve_ownership(
            LifecycleActionTermination(
                held[0].identity,
                worker.worker_identity,
                _identity("verified-terminal"),
                "owned-terminal",
                0,
            )
        )
        again = slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(again)
        again.release()

    def test_coalesced_bound_terminal_failure_never_becomes_waiter_authority(self):
        class Executor:
            dispatcher = None

            def action_key(inner, request):
                return _identity("complete-key")

            def produce(inner, request):
                until = time.monotonic() + 2
                while time.monotonic() < until:
                    with inner.dispatcher._lock:
                        users = sum(
                            len(value) for value in inner.dispatcher._users.values()
                        )
                    if users == 2:
                        error = ActionDagSchedulingError(
                            "worker-lost", "fixture owned process exited"
                        )
                        error.termination_evidence = LifecycleActionTermination(
                            request.identity,
                            request.worker.worker_identity,
                            _identity("producer-terminal"),
                            "owned-terminal",
                            1,
                        )
                        raise error
                    threading.Event().wait(0.001)
                raise TimeoutError("second fixture request never joined")

            def admit(inner, request, artifact):
                raise AssertionError("failed production must not admit")

            def cancel(inner, request):
                pass

        executor = Executor()
        dispatcher = CoalescingArtifactDispatcher(executor)
        executor.dispatcher = dispatcher
        result = LifecycleActionDagScheduler().run(
            (_node("one"), _node("two", workers=("beta",))),
            (_worker("alpha"), _worker("beta")),
            dispatcher,
            deadline_identity=_identity("deadline"),
        )
        self.assertEqual(len(result.attempts), 2)
        producer = next(
            attempt for attempt in result.attempts if attempt.termination is not None
        )
        waiter = next(
            attempt
            for attempt in result.attempts
            if attempt.producer_failure is not None
        )
        self.assertIsNone(waiter.termination)
        self.assertEqual(
            waiter.producer_failure.request_identity, waiter.result.request_identity
        )
        self.assertEqual(
            waiter.producer_failure.producer_request_identity,
            producer.result.request_identity,
        )
        self.assertEqual(
            waiter.producer_failure.evidence_identity,
            producer.termination.evidence_identity,
        )
        self.assertEqual(waiter.result.disposition, LifecycleActionDisposition.UNKNOWN)
        self.assertEqual(producer.result.disposition, LifecycleActionDisposition.FAILED)


if __name__ == "__main__":
    unittest.main()
