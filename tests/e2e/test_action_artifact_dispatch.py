"""A shared payload never copies acceptance between request envelopes."""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from literate_ai.application.action_artifact_dispatch import (
    CoalescingArtifactDispatcher,
)
from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDispatchOutcome,
    LifecycleActionDispatchRequest,
    LifecycleActionTermination,
)
from tests.e2e.test_action_dag_scheduler import _identity, _node, _worker


def request(name):
    return LifecycleActionDispatchRequest(
        _identity("schedule"),
        _node(name),
        _worker("alpha"),
        0,
        (),
        _identity("deadline"),
    )


class Executor:
    def __init__(self):
        self.produced = 0
        self.admitted = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.failed = False
        self.failure_terminal = True
        self.cancelled = []

    def action_key(self, request):
        return _identity("complete-semantic-key")

    def produce(self, request):
        self.produced += 1
        self.started.set()
        if not self.release.wait(2):
            raise TimeoutError("bounded test wait")
        if self.failed:
            error = RuntimeError("worker-lost")
            error.code = "worker-lost"
            if self.failure_terminal:
                error.termination_evidence = LifecycleActionTermination(
                    request.identity,
                    request.worker.worker_identity,
                    _identity("fixture-terminal"),
                    "owned-terminal",
                    1,
                )
            raise error
        return _identity("verified-payload")

    def admit(self, request, artifact):
        self.admitted.append(request.identity)
        return LifecycleActionDispatchOutcome(
            request.identity, _identity(request.identity.uri)
        )

    def cancel(self, request):
        self.cancelled.append(request.identity)
        self.release.set()


class ArtifactDispatchTests(unittest.TestCase):
    def test_one_producer_and_distinct_request_admission(self):
        executor = Executor()
        dispatcher = CoalescingArtifactDispatcher(executor)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(dispatcher.dispatch, request("one"))
            self.assertTrue(executor.started.wait(2))
            second = pool.submit(dispatcher.dispatch, request("two"))
            executor.release.set()
            a, b = first.result(), second.result()
        self.assertEqual(executor.produced, 1)
        self.assertEqual(len(executor.admitted), 2)
        self.assertNotEqual(a.request_identity, b.request_identity)
        self.assertNotEqual(a.result_identity, b.result_identity)

    def test_failed_producer_is_retained_as_failure_and_explicit_retry_produces(self):
        executor = Executor()
        executor.release.set()
        executor.failed = True
        dispatcher = CoalescingArtifactDispatcher(executor)
        with self.assertRaisesRegex(RuntimeError, "worker-lost"):
            dispatcher.dispatch(request("one"))
        self.assertEqual(executor.admitted, [])
        executor.failed = False
        dispatcher.dispatch(request("one"))
        self.assertEqual(executor.produced, 2)

    def test_cancelled_waiter_does_not_terminate_another_requests_producer(self):
        executor = Executor()
        dispatcher = CoalescingArtifactDispatcher(executor)
        first_request, second_request = request("one"), request("two")
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(dispatcher.dispatch, first_request)
            self.assertTrue(executor.started.wait(2))
            second = pool.submit(dispatcher.dispatch, second_request)
            until = time.monotonic() + 2
            while time.monotonic() < until:
                with dispatcher._lock:
                    users = sum(len(group) for group in dispatcher._users.values())
                if users == 2:
                    break
                threading.Event().wait(0.001)
            self.assertEqual(users, 2)
            dispatcher.cancel(first_request)
            self.assertEqual(executor.cancelled, [])
            executor.release.set()
            with self.assertRaises(ActionDagSchedulingError):
                first.result()
            self.assertEqual(second.result().request_identity, second_request.identity)
        self.assertEqual(executor.produced, 1)
        self.assertEqual(executor.admitted, [second_request.identity])

    def test_cancelling_every_user_reaches_owned_producer(self):
        executor = Executor()
        dispatcher = CoalescingArtifactDispatcher(executor)
        current = request("one")
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(dispatcher.dispatch, current)
            self.assertTrue(executor.started.wait(2))
            dispatcher.cancel(current)
            with self.assertRaises(ActionDagSchedulingError):
                future.result()
        self.assertEqual(executor.cancelled, [current.identity])
        self.assertEqual(executor.admitted, [])

    def test_unknown_production_key_cannot_restart_on_another_worker(self):
        executor = Executor()
        executor.release.set()
        executor.failed = True
        executor.failure_terminal = False
        dispatcher = CoalescingArtifactDispatcher(executor)
        original = request("one")
        with self.assertRaises(RuntimeError):
            dispatcher.dispatch(original)
        executor.failed = False
        with self.assertRaises(RuntimeError):
            dispatcher.dispatch(request("two"))
        self.assertEqual(executor.produced, 1)
        self.assertEqual(len(dispatcher.unresolved_productions), 1)
        dispatcher.resolve_production(
            executor.action_key(original),
            LifecycleActionTermination(
                original.identity,
                original.worker.worker_identity,
                _identity("later-owner-terminal"),
                "owned-terminal",
                0,
            ),
        )
        dispatcher.dispatch(request("two"))
        self.assertEqual(executor.produced, 2)
        self.assertEqual(dispatcher.unresolved_productions, ())

    def test_substituted_request_admission_refuses(self):
        executor = Executor()
        executor.release.set()
        executor.admit = lambda req, artifact: LifecycleActionDispatchOutcome(
            _identity("foreign-request"), artifact
        )
        with self.assertRaises(ActionDagSchedulingError):
            CoalescingArtifactDispatcher(executor).dispatch(request("one"))


if __name__ == "__main__":
    unittest.main()
