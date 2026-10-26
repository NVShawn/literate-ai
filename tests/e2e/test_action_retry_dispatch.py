"""Bounded declared-safe worker-loss retry retains every correlated attempt."""

from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace

from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDagScheduler,
    LifecycleActionDispatchOutcome,
    LifecycleActionDisposition,
    LifecycleActionKind,
    LifecycleActionTermination,
)
from literate_ai.application.action_dispatch_policy import ActionDispatchPolicy
from tests.e2e.test_action_dag_scheduler import (
    _identity,
    _node,
    _RecordingDispatcher,
    _worker,
)


class RetryDispatchTests(unittest.TestCase):
    def run_schedule(self, dispatcher, max_attempts=2):
        return LifecycleActionDagScheduler().run(
            (
                _node("build", workers=("alpha", "beta")),
                _node("child", predecessors=("build",), workers=("alpha", "beta")),
            ),
            (_worker("alpha"), _worker("beta")),
            dispatcher,
            deadline_identity=_identity("deadline"),
            action_policies={
                "build": ActionDispatchPolicy(
                    retry_safe=True,
                    max_attempts=max_attempts,
                    retryable_failure_codes=("worker-lost",),
                )
            },
        )

    def test_worker_loss_retries_other_worker_with_new_request_and_retained_failure(
        self,
    ):
        class Once(_RecordingDispatcher):
            def dispatch(inner, request):
                if request.action.action_id == "build":
                    inner.failures["build"] = (
                        "worker-lost" if not inner.requests else None
                    )
                outcome = super().dispatch(request)
                return (
                    replace(
                        outcome,
                        termination=LifecycleActionTermination(
                            request.identity,
                            request.worker.worker_identity,
                            _identity("owned-terminal"),
                            "owned-terminal",
                            1,
                        ),
                    )
                    if outcome.failure_code
                    else outcome
                )

        dispatcher = Once()
        result = self.run_schedule(dispatcher)
        builds = [
            request
            for request in dispatcher.requests
            if request.action.action_id == "build"
        ]
        self.assertEqual(
            [request.worker.worker_id for request in builds], ["alpha", "beta"]
        )
        self.assertNotEqual(builds[0].identity, builds[1].identity)
        self.assertEqual(builds[0].deadline_identity, builds[1].deadline_identity)
        attempts = [
            attempt
            for attempt in result.attempts
            if attempt.result.action_id == "build"
        ]
        self.assertEqual([attempt.number for attempt in attempts], [1, 2])
        self.assertEqual(attempts[0].result.failure_code, "worker-lost")
        self.assertEqual(
            attempts[1].result.disposition, LifecycleActionDisposition.ACCEPTED
        )
        self.assertTrue(
            all(
                item.disposition is LifecycleActionDisposition.ACCEPTED
                for item in result.results
            )
        )
        with self.assertRaises(ActionDagSchedulingError):
            replace(result, attempts=tuple(reversed(result.attempts)))

    def test_retry_budget_is_finite_and_descendants_cancel_only_after_exhaustion(self):
        class Terminal(_RecordingDispatcher):
            def dispatch(inner, request):
                outcome = super().dispatch(request)
                return replace(
                    outcome,
                    termination=LifecycleActionTermination(
                        request.identity,
                        request.worker.worker_identity,
                        _identity("owned-terminal"),
                        "owned-terminal",
                        1,
                    ),
                )

        dispatcher = Terminal({"build": "worker-lost"})
        result = self.run_schedule(dispatcher, 3)
        self.assertEqual(len(dispatcher.requests), 3)
        self.assertEqual(len(result.attempts), 3)
        self.assertEqual(
            result.results[0].disposition, LifecycleActionDisposition.FAILED
        )
        self.assertEqual(
            result.results[1].disposition, LifecycleActionDisposition.CANCELLED
        )

    def test_timeout_or_silence_is_unknown_and_never_authorizes_retry(self):
        dispatcher = _RecordingDispatcher({"build": "worker-lost"})
        result = self.run_schedule(dispatcher, 3)
        self.assertEqual(len(dispatcher.requests), 1)
        self.assertEqual(
            result.results[0].disposition, LifecycleActionDisposition.UNKNOWN
        )
        self.assertIsNone(result.attempts[0].termination)

    def test_fenced_loss_requires_the_exact_admitted_fencing_policy(self):
        class Fenced(_RecordingDispatcher):
            def dispatch(inner, request):
                outcome = super().dispatch(request)
                return replace(
                    outcome,
                    termination=LifecycleActionTermination(
                        request.identity,
                        request.worker.worker_identity,
                        _identity("fence-receipt"),
                        "fenced-lease",
                        fenced_lease_identity=_identity("old-lease"),
                        fencing_policy_identity=_identity("unadmitted-policy"),
                    ),
                )

        dispatcher = Fenced({"build": "worker-lost"})
        with self.assertRaises(ActionDagSchedulingError):
            self.run_schedule(dispatcher, 3)
        self.assertEqual(len(dispatcher.requests), 1)

    def test_other_failure_never_retries(self):
        dispatcher = _RecordingDispatcher({"build": "compile-failed"})
        result = self.run_schedule(dispatcher)
        self.assertEqual(len(dispatcher.requests), 1)
        self.assertEqual(result.results[0].failure_code, "compile-failed")

    def test_mutable_claims_or_unsafe_phases_refuse_retry_before_dispatch(self):
        for kwargs in (
            {"retry_safe": False},
            {"retry_safe": True, "exclusive_claims": ("signer",)},
        ):
            with self.assertRaises(ValueError):
                ActionDispatchPolicy(
                    max_attempts=2, retryable_failure_codes=("worker-lost",), **kwargs
                )
        dispatcher = _RecordingDispatcher()
        node = replace(_node("sign"), kind=LifecycleActionKind.PACKAGE)
        with self.assertRaises(ActionDagSchedulingError):
            LifecycleActionDagScheduler().run(
                (node,),
                (_worker("alpha"),),
                dispatcher,
                deadline_identity=_identity("deadline"),
                action_policies={
                    "sign": ActionDispatchPolicy(
                        retry_safe=True,
                        max_attempts=2,
                        retryable_failure_codes=("worker-lost",),
                    )
                },
            )
        self.assertEqual(dispatcher.requests, [])

    def test_retry_blocked_by_unresolved_ownership_returns_schedule_result(self):
        other_done = threading.Event()

        class Dispatcher:
            def __init__(self):
                self.requests = []

            def dispatch(self, request):
                self.requests.append(request)
                if request.action.action_id == "other":
                    other_done.set()
                    # Silence: no termination, so ownership stays unknown.
                    return LifecycleActionDispatchOutcome(
                        request.identity, None, "worker-lost"
                    )
                other_done.wait(5)
                time.sleep(0.2)
                return LifecycleActionDispatchOutcome(
                    request.identity,
                    None,
                    "worker-lost",
                    LifecycleActionTermination(
                        request.identity,
                        request.worker.worker_identity,
                        _identity("owned-terminal"),
                        "owned-terminal",
                        1,
                    ),
                )

        dispatcher = Dispatcher()
        scheduler = LifecycleActionDagScheduler()
        result = scheduler.run(
            (_node("build"), _node("child", predecessors=("build",)), _node("other")),
            (_worker("alpha", slots=2),),
            dispatcher,
            deadline_identity=_identity("deadline"),
            action_policies={
                "build": ActionDispatchPolicy(
                    retry_safe=True,
                    max_attempts=2,
                    retryable_failure_codes=("worker-lost",),
                )
            },
        )
        results = {item.action_id: item for item in result.results}
        self.assertEqual(
            results["build"].disposition, LifecycleActionDisposition.BLOCKED
        )
        self.assertEqual(
            results["child"].disposition, LifecycleActionDisposition.CANCELLED
        )
        self.assertEqual(
            results["other"].disposition, LifecycleActionDisposition.UNKNOWN
        )
        build_attempts = [
            item for item in result.attempts if item.result.action_id == "build"
        ]
        self.assertEqual(len(build_attempts), 1)
        self.assertEqual(
            build_attempts[0].result.disposition, LifecycleActionDisposition.FAILED
        )
        self.assertEqual(
            [item.request.action.action_id for item in result.unresolved_ownership],
            ["other"],
        )
        self.assertEqual(len(scheduler.unresolved_ownership), 1)
        # The blocked retry never dispatched a second attempt.
        self.assertEqual(
            sorted(item.action.action_id for item in dispatcher.requests),
            ["build", "other"],
        )


if __name__ == "__main__":
    unittest.main()
