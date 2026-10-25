"""A shared payload never copies acceptance between request envelopes."""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from literate_ai.application.action_artifact_dispatch import (
    CoalescingArtifactDispatcher,
)
from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDispatchOutcome,
    LifecycleActionDispatchRequest,
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

    def action_key(self, request):
        return _identity("complete-semantic-key")

    def produce(self, request):
        self.produced += 1
        self.started.set()
        if not self.release.wait(2):
            raise TimeoutError("bounded test wait")
        if self.failed:
            raise RuntimeError("worker-lost")
        return _identity("verified-payload")

    def admit(self, request, artifact):
        self.admitted.append(request.identity)
        return LifecycleActionDispatchOutcome(
            request.identity, _identity(request.identity.uri)
        )

    def cancel(self, request):
        pass


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
