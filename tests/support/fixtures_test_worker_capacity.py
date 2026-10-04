from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_worker_capacity``."""


from dataclasses import replace

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


def change_sample(observed, role, **changes):
    return replace(
        observed,
        samples=tuple(
            replace(sample, **changes) if sample.role == role else sample
            for sample in observed.samples
        ),
    )
