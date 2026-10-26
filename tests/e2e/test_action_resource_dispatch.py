"""Actual overlap and exclusion under aggregate reservations."""

from __future__ import annotations

import threading
import time
import unittest

from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDagScheduler,
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


class ResourceDispatchTests(unittest.TestCase):
    def schedule(self, dispatcher, policies, capacities, workers=None):
        return LifecycleActionDagScheduler().run(
            tuple(
                _node(name, workers=("alpha", "beta"))
                for name in ("one", "two", "three")
            ),
            workers or (_worker("alpha", slots=3), _worker("beta")),
            dispatcher,
            deadline_identity=_identity("deadline"),
            action_policies=policies,
            worker_resources=capacities,
        )

    def test_resource_reservations_and_shared_claims_bound_real_overlap(self):
        class Dispatcher(_RecordingDispatcher):
            active = 0
            maximum = 0

            def dispatch(inner, request):
                with inner.lock:
                    inner.active += 1
                    inner.maximum = max(inner.maximum, inner.active)
                time.sleep(0.02)
                with inner.lock:
                    inner.active -= 1
                return super().dispatch(request)

        for field in ("cpu_millis", "memory_mib", "disk_mib", "network_mbps", "claim"):
            with self.subTest(field=field):
                dispatcher = Dispatcher()
                policy = (
                    ActionDispatchPolicy(exclusive_claims=("signing-authority",))
                    if field == "claim"
                    else ActionDispatchPolicy(ActionResources(**{field: 2}))
                )
                capacities = (
                    {key: ActionResources(**{field: 2}) for key in ("alpha", "beta")}
                    if field != "claim"
                    else {}
                )
                self.schedule(
                    dispatcher,
                    {name: policy for name in ("one", "two", "three")},
                    capacities,
                )
                self.assertEqual(dispatcher.maximum, 1 if field == "claim" else 2)

    def test_failure_releases_claim_for_independent_action(self):
        dispatcher = _RecordingDispatcher({"one": "build-failed"})
        policies = {
            name: ActionDispatchPolicy(exclusive_claims=("pkgdb:target",))
            for name in ("one", "two", "three")
        }
        result = self.schedule(dispatcher, policies, {})
        self.assertEqual(len(dispatcher.requests), 3)
        self.assertEqual(
            sum(item.failure_code is not None for item in result.results), 1
        )

    def test_distinct_package_database_and_signer_claims_do_not_serialize(self):
        barrier = threading.Barrier(2, timeout=5)

        class Dispatcher(_RecordingDispatcher):
            active: set[str] = set()
            overlaps: list[frozenset[str]] = []

            def dispatch(inner, request):
                action_id = request.action.action_id
                with inner.lock:
                    inner.active.add(action_id)
                    inner.overlaps.append(frozenset(inner.active))
                try:
                    if action_id in {"one", "two"}:
                        barrier.wait()
                    else:
                        time.sleep(0.02)
                finally:
                    with inner.lock:
                        inner.active.discard(action_id)
                return super().dispatch(request)

        dispatcher = Dispatcher()
        result = self.schedule(
            dispatcher,
            {
                "one": ActionDispatchPolicy(exclusive_claims=("pkgdb:target",)),
                "two": ActionDispatchPolicy(exclusive_claims=("signing-authority",)),
                "three": ActionDispatchPolicy(
                    exclusive_claims=("pkgdb:target", "signing-authority")
                ),
            },
            {},
        )
        self.assertEqual(len(dispatcher.requests), 3)
        self.assertTrue(all(item.failure_code is None for item in result.results))
        self.assertTrue(
            all(
                "three" not in seen or seen == {"three"} for seen in dispatcher.overlaps
            )
        )

    def test_impossible_reservation_refuses_before_any_dispatch(self):
        dispatcher = _RecordingDispatcher()
        with self.assertRaises(ActionDagSchedulingError) as caught:
            self.schedule(
                dispatcher,
                {"one": ActionDispatchPolicy(ActionResources(memory_mib=3))},
                {"alpha": ActionResources(memory_mib=2)},
            )
        self.assertEqual(caught.exception.code, "action_dag.capacity_insufficient")
        self.assertEqual(dispatcher.requests, [])

    def test_policy_changes_invalidate_schedule_and_recovery_authority(self):
        first = self.schedule(_RecordingDispatcher(), {}, {})
        second = self.schedule(
            _RecordingDispatcher(),
            {"one": ActionDispatchPolicy(exclusive_claims=("pkgdb:target",))},
            {},
        )
        self.assertNotEqual(first.schedule_identity, second.schedule_identity)

    def test_invalid_or_unknown_policy_refused(self):
        for policies, capacities in (
            ({"unknown": ActionDispatchPolicy()}, {}),
            ({}, {"unknown": ActionResources()}),
            ({"one": None}, {}),
        ):
            with self.assertRaises(ActionDagSchedulingError):
                self.schedule(_RecordingDispatcher(), policies, capacities)
        for value in (-1, True, 2**64):
            with self.assertRaises(ValueError):
                ActionResources(memory_mib=value)


if __name__ == "__main__":
    unittest.main()
