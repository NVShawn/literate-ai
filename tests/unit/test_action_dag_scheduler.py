"""Action-level DAG scheduling, failure isolation, and recovery authority."""

from __future__ import annotations

import threading
import unittest
from collections import defaultdict

from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDagScheduler,
    LifecycleActionDispatchOutcome,
    LifecycleActionDisposition,
    LifecycleActionKind,
    LifecycleActionNode,
    LifecycleActionRecoveryCandidate,
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

    def test_dispatches_newly_ready_action_before_unrelated_root_finishes(self):
        slow_started = threading.Event()
        dependent_started = threading.Event()

        class Dispatcher(_RecordingDispatcher):
            def dispatch(inner_self, request):
                with inner_self.lock:
                    inner_self.requests.append(request)
                if request.action.action_id == "slow":
                    slow_started.set()
                    if not dependent_started.wait(timeout=5):
                        raise RuntimeError("dependent action did not overlap slow root")
                if request.action.action_id == "after-quick":
                    self.assertTrue(slow_started.is_set())
                    dependent_started.set()
                return LifecycleActionDispatchOutcome(
                    request.identity,
                    _identity(f"result-{request.action.action_id}"),
                )

        dispatcher = Dispatcher()
        result = self.scheduler.run(
            (
                _node("slow", workers=("alpha", "beta")),
                _node("quick", workers=("alpha", "beta")),
                _node(
                    "after-quick",
                    predecessors=("quick",),
                    workers=("alpha", "beta"),
                ),
            ),
            (_worker("alpha"), _worker("beta")),
            dispatcher,
            deadline_identity=self.deadline,
        )

        self.assertTrue(dependent_started.is_set())
        self.assertEqual(
            {item.disposition for item in result.results},
            {LifecycleActionDisposition.ACCEPTED},
        )

    def test_honors_worker_slots_and_explicit_parallelism_cap(self):
        active = defaultdict(int)
        peaks = defaultdict(int)
        total_active = 0
        peak_total = 0
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        class Dispatcher(_RecordingDispatcher):
            def dispatch(inner_self, request):
                nonlocal total_active, peak_total
                with lock:
                    active[request.worker.worker_id] += 1
                    peaks[request.worker.worker_id] = max(
                        peaks[request.worker.worker_id],
                        active[request.worker.worker_id],
                    )
                    total_active += 1
                    peak_total = max(peak_total, total_active)
                barrier.wait(timeout=5)
                with lock:
                    active[request.worker.worker_id] -= 1
                    total_active -= 1
                return LifecycleActionDispatchOutcome(
                    request.identity, _identity(request.action.action_id)
                )

        nodes = tuple(
            _node(f"root-{index}", workers=("alpha", "beta")) for index in range(4)
        )
        self.scheduler.run(
            nodes,
            (_worker("alpha", slots=2), _worker("beta", slots=2)),
            Dispatcher(),
            deadline_identity=self.deadline,
            max_parallelism=2,
        )

        self.assertEqual(peak_total, 2)
        self.assertLessEqual(peaks["alpha"], 2)
        self.assertLessEqual(peaks["beta"], 2)

    def test_prefers_cache_affinity_then_stable_worker_and_slot(self):
        dispatcher = _RecordingDispatcher()
        self.scheduler.run(
            (
                _node(
                    "affine",
                    workers=("alpha", "beta"),
                    affinity=("beta",),
                ),
                _node(
                    "stable",
                    predecessors=("affine",),
                    workers=("alpha", "beta"),
                ),
            ),
            (_worker("beta", slots=2), _worker("alpha", slots=2)),
            dispatcher,
            deadline_identity=self.deadline,
        )

        by_action = {item.action.action_id: item for item in dispatcher.requests}
        self.assertEqual(by_action["affine"].worker.worker_id, "beta")
        self.assertEqual(by_action["affine"].slot, 0)
        self.assertEqual(by_action["stable"].worker.worker_id, "alpha")
        self.assertEqual(by_action["stable"].slot, 0)

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

    def test_recovery_requires_exact_request_and_worker_route(self):
        node = _node("build")
        worker = _worker("alpha")
        dispatcher = _RecordingDispatcher()
        first = self.scheduler.run(
            (node,),
            (worker,),
            dispatcher,
            deadline_identity=self.deadline,
        )
        accepted = first.results[0]
        candidate = LifecycleActionRecoveryCandidate(
            accepted.request_identity,
            accepted.worker_identity,
            accepted.result_identity,
        )
        recovered = self.scheduler.run(
            (node,),
            (worker,),
            _RecordingDispatcher(),
            deadline_identity=self.deadline,
            recovery={"build": candidate},
        )
        self.assertEqual(
            recovered.results[0].disposition,
            LifecycleActionDisposition.RECOVERED,
        )

        changed_worker = _worker("alpha", slots=2)
        with self.assertRaisesRegex(ActionDagSchedulingError, "recovery route"):
            self.scheduler.run(
                (node,),
                (changed_worker,),
                _RecordingDispatcher(),
                deadline_identity=self.deadline,
                recovery={"build": candidate},
            )

    def test_rejects_dispatch_evidence_for_another_request(self):
        class Dispatcher(_RecordingDispatcher):
            def dispatch(inner_self, request):
                return LifecycleActionDispatchOutcome(
                    _identity("wrong-request"), _identity("result")
                )

        with self.assertRaisesRegex(ActionDagSchedulingError, "another request"):
            self.scheduler.run(
                (_node("build"),),
                (_worker("alpha"),),
                Dispatcher(),
                deadline_identity=self.deadline,
            )

    def test_rejects_cycles_unknown_edges_and_unknown_workers(self):
        cases = (
            (
                (_node("a", predecessors=("b",)), _node("b", predecessors=("a",))),
                (_worker("alpha"),),
                "directed cycle",
            ),
            (
                (_node("a", predecessors=("missing",)),),
                (_worker("alpha"),),
                "unknown",
            ),
            (
                (_node("a", workers=("missing",)),),
                (_worker("alpha"),),
                "unknown eligible worker",
            ),
        )
        for nodes, workers, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ActionDagSchedulingError, message):
                    self.scheduler.run(
                        nodes,
                        workers,
                        _RecordingDispatcher(),
                        deadline_identity=self.deadline,
                    )


if __name__ == "__main__":
    unittest.main()
