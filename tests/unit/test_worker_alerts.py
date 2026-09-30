"""Alert history deduplicates reporting without granting admission or recovery."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai._cache_lock import exclusive_cache_lock
from literate_ai.adapters.worker_alerts import (
    WorkerAlertHistoryError,
    record_worker_alerts,
)
from literate_ai.application.worker_alerts import (
    RETENTION_MS,
    WorkerAlertHistory,
    alert_transitions,
)
from literate_ai.application.worker_capacity import (
    CapacityHealth,
    assess_worker_capacity,
)
from literate_ai.application.worker_pressure import PressureFinding
from literate_ai.contracts import (
    CapacityMetric,
    CapacityProbeStatus,
    QuotaCapacitySample,
    canonical_identity,
)
from tests.unit.test_worker_capacity import (
    JOB,
    NOT_APPLICABLE,
    NOW,
    change_sample,
    observation,
    policy,
)


def measured(selected, tick=0, available=110):
    value = observation(selected, available=available)
    value = replace(
        value,
        started_at_ms=value.started_at_ms + tick,
        completed_at_ms=value.completed_at_ms + tick,
        expires_at_ms=value.expires_at_ms + tick,
    )
    assessment = assess_worker_capacity(
        selected, value, worker_id=value.worker_id, job_identity=JOB, now_ms=NOW + tick
    )
    return assessment, value


class WorkerAlertTransitionsTests(unittest.TestCase):
    def setUp(self):
        self.policy = replace(policy(reserve_bytes=100), warning_headroom_bytes=20)

    def step(self, previous=None, *, tick=0, available=110):
        assessment, value = measured(self.policy, tick, available)
        return alert_transitions(assessment, value, previous)

    def test_numeric_jitter_deduplicates_escalation_improvement_and_recovery_emit(self):
        state, events, meta = self.step()
        self.assertEqual(len(events), 4)
        self.assertEqual({e["transition"] for e in events}, {"initial"})
        state, events, meta = self.step(state, tick=1, available=115)
        self.assertEqual(events, [])
        self.assertEqual(meta["suppressed"], 4)
        state, events, _ = self.step(state, tick=2, available=99)
        self.assertEqual({e["transition"] for e in events}, {"escalated"})
        state, events, _ = self.step(state, tick=3, available=110)
        self.assertEqual({e["transition"] for e in events}, {"improved"})
        state, events, _ = self.step(state, tick=4, available=120)
        self.assertEqual({e["transition"] for e in events}, {"recovered"})
        self.assertEqual(len(events), 4)
        _, events, _ = self.step(state, tick=5, available=1000)
        self.assertEqual(events, [])

    def test_missing_measurement_cannot_report_recovery(self):
        state, _, _ = self.step(available=99)
        _, value = measured(self.policy, 1, 99)
        value = replace(
            value, samples=tuple(s for s in value.samples if s.role != "temp")
        )
        assessment = assess_worker_capacity(
            self.policy,
            value,
            worker_id=value.worker_id,
            job_identity=JOB,
            now_ms=NOW + 1,
        )
        state, events, _ = alert_transitions(assessment, value, state)
        affected = [e for e in events if e["role"] == "temp"]
        self.assertTrue(affected)
        self.assertNotIn("recovered", {e["transition"] for e in affected})
        self.assertEqual(
            next(m.health for m in state.metrics if m.key == ("temp", "bytes")),
            "unknown",
        )

    def test_positive_quota_inapplicability_can_recover_a_failed_query(self):
        _, value = measured(self.policy)
        value = change_sample(
            value,
            "temp",
            quotas=(
                QuotaCapacitySample(
                    "quota", CapacityMetric(CapacityProbeStatus.DENIED), NOT_APPLICABLE
                ),
            ),
        )
        assessment = assess_worker_capacity(
            self.policy, value, worker_id=value.worker_id, job_identity=JOB, now_ms=NOW
        )
        state, _, _ = alert_transitions(assessment, value)
        _, events, _ = self.step(state, tick=1)
        self.assertIn(
            ("temp", "quota", "recovered"),
            [(e["role"], e["resource"], e["transition"]) for e in events],
        )

    def test_context_and_expiry_reset_instead_of_suppressing_new_incidents(self):
        state, _, _ = self.step()
        state = replace(state, policy_identity=canonical_identity("old policy"))
        _, events, meta = self.step(state, tick=1)
        self.assertEqual(meta["reset_reason"], "context-changed")
        self.assertEqual(len(events), 4)
        state, _, _ = self.step()
        _, events, meta = self.step(state, tick=RETENTION_MS)
        self.assertEqual(meta["reset_reason"], "history-expired")
        self.assertEqual(len(events), 4)

    def test_foreign_future_and_out_of_order_histories_refuse(self):
        state, _, _ = self.step(tick=3)
        cases = [
            replace(state, worker_id="other"),
            replace(state, recorded_at_ms=NOW + 100),
        ]
        for previous in cases:
            with self.assertRaises(ValueError):
                self.step(previous, tick=4)
        with self.assertRaisesRegex(ValueError, "out_of_order"):
            assessment, value = measured(self.policy, 1)
            alert_transitions(replace(assessment, assessed_at_ms=NOW + 4), value, state)

    def test_identical_observation_replay_is_idempotent(self):
        state, _, _ = self.step()
        next_state, events, _ = self.step(state)
        self.assertEqual(state, next_state)
        self.assertEqual(events, [])

    def test_pressure_alerts_deduplicate_and_report_recovery(self):
        critical = PressureFinding(
            "memory",
            CapacityHealth.CRITICAL,
            "sustained-memory-pressure",
            True,
            512,
            1024,
        )
        assessment, value = measured(self.policy)
        state, events, _ = alert_transitions(
            assessment, value, additional_findings=(critical,)
        )
        pressure = [event for event in events if event["role"] == "worker"]
        self.assertEqual(len(pressure), 1)
        self.assertEqual(pressure[0]["transition"], "initial")

        assessment, value = measured(self.policy, 1)
        state, events, metadata = alert_transitions(
            assessment,
            value,
            state,
            additional_findings=(critical,),
        )
        self.assertFalse([event for event in events if event["role"] == "worker"])
        self.assertGreaterEqual(metadata["suppressed"], 1)

        healthy = replace(
            critical,
            health=CapacityHealth.HEALTHY,
            reason="memory-sufficient",
            measured=4096,
        )
        assessment, value = measured(self.policy, 2)
        _, events, _ = alert_transitions(
            assessment,
            value,
            state,
            additional_findings=(healthy,),
        )
        pressure = [event for event in events if event["role"] == "worker"]
        self.assertEqual(len(pressure), 1)
        self.assertEqual(pressure[0]["transition"], "recovered")

    def test_schema_registry_accepts_history_events_and_inode_quota_findings(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        state, events, _ = self.step()
        schemas = Path(__file__).resolve().parents[2] / "schemas"
        registry = Registry()
        for path in schemas.glob("v*/*.schema.json"):
            document = json.loads(path.read_text())
            registry = registry.with_resource(
                document["$id"], Resource.from_contents(document)
            )
        registry = registry.crawl()
        for value in (state.to_dict(), *events):
            Draft202012Validator({"$ref": value["schema"]}, registry=registry).validate(
                value
            )
        assessment, observation_value = measured(self.policy, 1)
        _, pressure_events, _ = alert_transitions(
            assessment,
            observation_value,
            state,
            additional_findings=(
                PressureFinding(
                    "gpu",
                    CapacityHealth.CRITICAL,
                    "gpu-allocation-failed",
                    False,
                ),
            ),
        )
        for value in pressure_events:
            Draft202012Validator({"$ref": value["schema"]}, registry=registry).validate(
                value
            )
        assessment, _ = measured(self.policy)
        value = assessment.to_dict()
        self.assertIn("quota-inodes", {f["resource"] for f in value["findings"]})
        Draft202012Validator({"$ref": value["schema"]}, registry=registry).validate(
            value
        )

    def test_bounded_strict_state_roundtrip_and_private_content_refusal(self):
        state, _, _ = self.step()
        self.assertEqual(WorkerAlertHistory.from_dict(state.to_dict()), state)
        for mutation in (
            {"private-path": "/private/path"},
            {"metrics": state.to_dict()["metrics"] * 5},
            {"recorded_at_ms": True},
        ):
            with self.assertRaises(ValueError):
                WorkerAlertHistory.from_dict({**state.to_dict(), **mutation})
        assessment, value = measured(self.policy)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            alert_transitions(replace(assessment, worker_id="other"), value)


class WorkerAlertPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wa-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "alerts.json"
        self.assessment, self.observation = measured(
            replace(policy(reserve_bytes=100), warning_headroom_bytes=20)
        )

    def record(self, **kwargs):
        return record_worker_alerts(
            self.path, self.assessment, self.observation, **kwargs
        )

    def test_private_atomic_file_deduplicates_and_keeps_only_bounded_state(self):
        events, _ = self.record()
        self.assertEqual(len(events), 4)
        self.assertLess(self.path.stat().st_size, 65536)
        self.assertEqual(self.record()[0], [])
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            [".alerts.json.lock", "alerts.json"],
        )
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        content = self.path.read_text()
        self.assertNotIn(str(self.root), content)
        self.assertNotIn("available", content)
        WorkerAlertHistory.from_dict(json.loads(content))

    def test_concurrent_identical_observations_emit_only_one_initial_event_set(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(lambda _: self.record(), range(2)))
        self.assertEqual(sorted(len(events) for events, _ in outputs), [0, 4])

    def test_corrupt_foreign_and_oversized_states_are_preserved(self):
        self.record()
        valid = json.loads(self.path.read_text())
        for raw in (
            b"corrupt",
            b"[" * 2000 + b"]" * 2000,
            b" " * 65537,
            json.dumps({**valid, "worker_id": "other"}).encode(),
        ):
            self.path.write_bytes(raw)
            with self.assertRaises(WorkerAlertHistoryError):
                self.record()
            self.assertEqual(self.path.read_bytes(), raw)

    def test_lock_contention_is_finite_and_does_not_publish(self):
        with exclusive_cache_lock(self.root / ".alerts.json.lock"):
            with self.assertRaises(WorkerAlertHistoryError):
                self.record(lock_timeout_seconds=0.01)
        self.assertFalse(self.path.exists())

    def test_input_guard_failure_and_write_failure_leave_no_partial_state(self):
        with self.assertRaises(WorkerAlertHistoryError):
            self.record(guard=lambda: (_ for _ in ()).throw(ValueError("changed")))
        self.assertFalse(self.path.exists())
        self.record()
        previous = self.path.read_bytes()
        with (
            patch(
                "literate_ai.adapters.worker_alerts.os.replace",
                side_effect=OSError("failed"),
            ),
            self.assertRaises(WorkerAlertHistoryError),
        ):
            self.record()
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertFalse(list(self.root.glob(".worker-alert-*")))

    def test_late_noncooperating_change_is_preserved_and_stage_removed(self):
        self.record()
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.path.write_text("concurrent replacement")

        with self.assertRaises(WorkerAlertHistoryError):
            self.record(guard=guard)
        self.assertEqual(self.path.read_text(), "concurrent replacement")
        self.assertFalse(list(self.root.glob(".worker-alert-*")))

    def test_symlink_parent_is_rejected_before_creating_a_lock(self):
        directory = self.root / "actual"
        directory.mkdir()
        alias = self.root / "alias"
        try:
            alias.symlink_to(directory, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symlinks")
        self.path = alias / "state.json"
        with self.assertRaises(WorkerAlertHistoryError):
            self.record()
        self.assertEqual(list(directory.iterdir()), [])

    def test_symlink_and_hardlink_state_refuse_without_touching_targets(self):
        target = self.root / "protected"
        target.write_text("preserve")
        try:
            self.path.symlink_to(target)
        except OSError:
            self.skipTest("host does not permit symlinks")
        with self.assertRaises(WorkerAlertHistoryError):
            self.record()
        self.assertEqual(target.read_text(), "preserve")
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises(WorkerAlertHistoryError):
            self.record()
        self.assertEqual(target.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
