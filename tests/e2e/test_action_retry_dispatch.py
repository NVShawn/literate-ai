"""Bounded declared-safe worker-loss retry retains every correlated attempt."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDagScheduler,
    LifecycleActionDisposition,
    LifecycleActionKind,
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
                return super().dispatch(request)

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
        dispatcher = _RecordingDispatcher({"build": "worker-lost"})
        result = self.run_schedule(dispatcher, 3)
        self.assertEqual(len(dispatcher.requests), 3)
        self.assertEqual(len(result.attempts), 3)
        self.assertEqual(
            result.results[0].disposition, LifecycleActionDisposition.FAILED
        )
        self.assertEqual(
            result.results[1].disposition, LifecycleActionDisposition.CANCELLED
        )

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


if __name__ == "__main__":
    unittest.main()
