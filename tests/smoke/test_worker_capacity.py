"""Capacity admission must not invent capacity, identity, or cleanup authority."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.application.worker_capacity import (
    CapacityDecision,
    CapacityHealth,
    assess_worker_capacity,
)
from literate_ai.contracts import (
    CapacityMetric,
    CapacityProbeStatus,
    QuotaCapacitySample,
    StorageCapacitySample,
    StorageRoleCapacityPolicy,
    WorkerCapacityObservation,
    WorkerCapacityPolicy,
    canonical_identity,
)

ROLES = ("cache", "output", "temp", "workspace")
JOB = canonical_identity({"job": "capacity-fixture"})
NOW = 100_000
MEASURED = CapacityProbeStatus.MEASURED
NOT_APPLICABLE = CapacityMetric(CapacityProbeStatus.NOT_APPLICABLE)


def policy(**role_changes):
    return WorkerCapacityPolicy(
        tuple(
            replace(
                StorageRoleCapacityPolicy(role, role, 0, 0, 0, 0, 0, True, False),
                **role_changes,
            )
            for role in ROLES
        ),
        True,
        30_000,
        5_000,
        2,
        0,
        canonical_identity({"private_role_bindings": "fixture"}),
    )


def observation(selected, *, available=1000, total=2000, separate=False):
    return WorkerCapacityObservation(
        "worker.fixture",
        selected.identity,
        JOB,
        "linux",
        NOW - 100,
        NOW - 1,
        NOW + 10_000,
        tuple(
            StorageCapacitySample(
                role,
                role if separate else "volume-1",
                total,
                CapacityMetric(MEASURED, available),
                NOT_APPLICABLE,
                (QuotaCapacitySample("no-quota", NOT_APPLICABLE, NOT_APPLICABLE),),
            )
            for role in ROLES
        ),
    )


def assess(selected, observed=None, *, attempt=0, **options):
    return assess_worker_capacity(
        selected,
        observation(selected) if observed is None else observed,
        worker_id="worker.fixture",
        job_identity=JOB,
        now_ms=NOW,
        attempt=attempt,
        **options,
    )


def change_sample(observed, role, **changes):
    return replace(
        observed,
        samples=tuple(
            replace(sample, **changes) if sample.role == role else sample
            for sample in observed.samples
        ),
    )


class WorkerCapacityTests(unittest.TestCase):
    def test_separate_volumes_never_pool_free_space(self):
        selected = policy(additional_bytes=20, reserve_bytes=10)
        observed = change_sample(
            observation(selected, separate=True),
            "temp",
            available_bytes=CapacityMetric(MEASURED, 29),
        )
        result = assess(selected, observed)
        self.assertEqual(result.decision, CapacityDecision.HOLD)
        deficits = [
            item for item in result.findings if item.health is CapacityHealth.CRITICAL
        ]
        self.assertEqual(
            [(item.roles, item.minimum) for item in deficits], [(("temp",), 30)]
        )
        self.assertEqual(
            assess(
                selected,
                change_sample(
                    observed, "temp", available_bytes=CapacityMetric(MEASURED, 30)
                ),
            ).decision,
            CapacityDecision.PROCEED,
        )

    def test_quota_exhaustion_holds_even_with_healthy_filesystem_capacity(self):
        selected = policy(additional_bytes=1)
        observed = change_sample(
            observation(selected),
            "cache",
            quotas=(
                QuotaCapacitySample(
                    "cache-quota", CapacityMetric(MEASURED, 0), NOT_APPLICABLE
                ),
            ),
        )
        result = assess(selected, observed)
        self.assertEqual(result.health, CapacityHealth.CRITICAL)
        self.assertEqual(result.decision, CapacityDecision.HOLD)
        self.assertTrue(
            any(
                item.resource == "bytes" and item.health is CapacityHealth.HEALTHY
                for item in result.findings
            )
        )


if __name__ == "__main__":
    unittest.main()
