"""Action-level DAG scheduling, failure isolation, and recovery authority."""

from __future__ import annotations

import threading
import unittest

from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDagScheduler,
    LifecycleActionDispatchOutcome,
    LifecycleActionDisposition,
    LifecycleActionKind,
    LifecycleActionNode,
    LifecycleActionWorker,
)
from literate_ai.contracts.identity import canonical_identity


def _identity(label: str):
    return canonical_identity({"test": label})


def _worker(worker_id: str, *, slots: int = 1) -> LifecycleActionWorker:
    return LifecycleActionWorker(
        worker_id,
        _identity(f"worker-{worker_id}"),
        _identity(f"catalog-{worker_id}"),
        _identity(f"observation-{worker_id}"),
        slots,
    )


def _node(
    action_id: str,
    *,
    predecessors: tuple[str, ...] = (),
    workers: tuple[str, ...] = ("alpha",),
    affinity: tuple[str, ...] = (),
) -> LifecycleActionNode:
    return LifecycleActionNode(
        action_id,
        _identity(f"component-{action_id}"),
        LifecycleActionKind.BUILD,
        _identity(f"payload-{action_id}"),
        predecessors,
        workers,
        affinity,
    )


class _RecordingDispatcher:
    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.failures = {} if failures is None else failures
        self.requests = []
        self.cancelled = []
        self.lock = threading.Lock()

    def dispatch(self, request):
        with self.lock:
            self.requests.append(request)
        failure = self.failures.get(request.action.action_id)
        return LifecycleActionDispatchOutcome(
            request.identity,
            None if failure else _identity(f"result-{request.action.action_id}"),
            failure,
        )

    def cancel(self, request):
        with self.lock:
            self.cancelled.append(request)


class ActionDagSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = LifecycleActionDagScheduler()
        self.deadline = _identity("deadline")

    def test_failure_cancels_only_transitive_descendants(self):
        dispatcher = _RecordingDispatcher({"failed": "build-failed"})
        result = self.scheduler.run(
            (
                _node("failed"),
                _node("child", predecessors=("failed",)),
                _node("grandchild", predecessors=("child",)),
                _node("independent"),
            ),
            (_worker("alpha", slots=2),),
            dispatcher,
            deadline_identity=self.deadline,
        )

        results = {item.action_id: item for item in result.results}
        self.assertEqual(
            results["failed"].disposition, LifecycleActionDisposition.FAILED
        )
        self.assertEqual(
            results["child"].disposition, LifecycleActionDisposition.CANCELLED
        )
        self.assertEqual(
            results["grandchild"].disposition,
            LifecycleActionDisposition.CANCELLED,
        )
        self.assertEqual(
            results["independent"].disposition,
            LifecycleActionDisposition.ACCEPTED,
        )
        self.assertEqual(
            {item.action.action_id for item in dispatcher.requests},
            {"failed", "independent"},
        )


if __name__ == "__main__":
    unittest.main()
