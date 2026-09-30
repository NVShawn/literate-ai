"""Deterministic storage admission over fresh, explicitly bound observations.

This module performs no probes, dispatch, cancellation, or cleanup. Platform adapters
must supply caller-available bytes and stable private volume/quota aliases.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from literate_ai.contracts._validation import fail, int_value
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.worker_capacity import (
    CapacityMetric,
    CapacityProbeStatus,
    QuotaCapacitySample,
    StorageCapacitySample,
    StorageRoleCapacityPolicy,
    WorkerCapacityObservation,
    WorkerCapacityPolicy,
    capacity_alias,
)


class CapacityHealth(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    UNKNOWN = "unknown"
    CRITICAL = "critical"


class CapacityDecision(StrEnum):
    PROCEED = "proceed"
    HOLD = "hold"
    RETRY_AFTER_RECHECK = "retry-after-recheck"


@dataclass(frozen=True, slots=True)
class CapacityFinding:
    roles: tuple[str, ...]
    resource: str
    health: CapacityHealth
    reason: str
    required: bool
    available: int | None = None
    minimum: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "roles": list(self.roles),
            "resource": self.resource,
            "health": self.health.value,
            "reason": self.reason,
            "required": self.required,
            "available": self.available,
            "minimum": self.minimum,
            "deficit": None
            if self.available is None or self.minimum is None
            else max(0, self.minimum - self.available),
        }


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    worker_id: str
    policy_identity: ContentIdentity
    observation_identity: ContentIdentity
    job_identity: ContentIdentity | None
    assessed_at_ms: int
    observation_expires_at_ms: int
    health: CapacityHealth
    decision: CapacityDecision
    attempt: int
    findings: tuple[CapacityFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/worker-capacity-assessment@1",
            "worker_id": self.worker_id,
            "policy_identity": self.policy_identity.uri,
            "observation_identity": self.observation_identity.uri,
            "job_identity": None
            if self.job_identity is None
            else self.job_identity.uri,
            "assessed_at_ms": self.assessed_at_ms,
            "observation_expires_at_ms": self.observation_expires_at_ms,
            "health": self.health.value,
            "decision": self.decision.value,
            "attempt": self.attempt,
            "findings": [item.to_dict() for item in self.findings],
        }


_HEALTH_ORDER = {value: index for index, value in enumerate(CapacityHealth)}
_Group = list[tuple[StorageRoleCapacityPolicy, StorageCapacitySample]]


def _unknown(
    role: str, resource: str, metric: CapacityMetric, required: bool
) -> CapacityFinding:
    return CapacityFinding(
        (role,),
        resource,
        CapacityHealth.UNKNOWN,
        f"probe-{metric.status.value}",
        required,
    )


def _budget(
    roles: tuple[str, ...], resource: str, available: int, minimum: int, warning: int
) -> CapacityFinding:
    if minimum > 2**63 - 1:
        return CapacityFinding(
            roles,
            resource,
            CapacityHealth.CRITICAL,
            "demand-exceeds-supported-range",
            True,
            available,
        )
    if available < minimum:
        health, reason = CapacityHealth.CRITICAL, "capacity-deficit"
    elif available - minimum < warning:
        health, reason = CapacityHealth.WARNING, "reserve-margin-low"
    else:
        health, reason = CapacityHealth.HEALTHY, "capacity-sufficient"
    return CapacityFinding(roles, resource, health, reason, True, available, minimum)


def _volume_findings(
    group: _Group, policy: WorkerCapacityPolicy
) -> list[CapacityFinding]:
    roles = tuple(item.role for item, _sample in group)
    findings: list[CapacityFinding] = []
    measured = [
        sample
        for _item, sample in group
        if sample.available_bytes.status is CapacityProbeStatus.MEASURED
    ]
    totals = {sample.total_bytes for sample in measured}
    if len(totals) > 1:
        findings.append(
            CapacityFinding(
                roles,
                "bytes",
                CapacityHealth.UNKNOWN,
                "conflicting-volume-capacity",
                True,
            )
        )
    elif measured:
        total = measured[0].total_bytes
        assert total is not None
        additional = sum(item.additional_bytes for item, _sample in group)
        reserve = max(item.reserve_bytes for item, _sample in group)
        basis_points = max(item.minimum_free_basis_points for item, _sample in group)
        # The percentage floor must remain available AFTER concurrent allocation.
        reserve = max(reserve, (total * basis_points + 9999) // 10000)
        available = min(sample.available_bytes.value for sample in measured)
        assert available is not None
        findings.append(
            _budget(
                roles,
                "bytes",
                available,
                additional + reserve,
                policy.warning_headroom_bytes,
            )
        )
    inode_samples = [
        sample.available_inodes.value
        for _item, sample in group
        if sample.available_inodes.status is CapacityProbeStatus.MEASURED
    ]
    if inode_samples:
        available_inodes = min(inode_samples)
        assert available_inodes is not None
        needed = sum(item.additional_inodes for item, _sample in group) + max(
            item.reserve_inodes for item, _sample in group
        )
        findings.append(_budget(roles, "inodes", available_inodes, needed, 0))
    return findings


def assess_worker_capacity(
    policy: WorkerCapacityPolicy,
    observation: WorkerCapacityObservation,
    *,
    worker_id: str,
    job_identity: ContentIdentity | None,
    now_ms: int,
    attempt: int = 0,
) -> CapacityAssessment:
    """Classify one attempt; retry decisions never execute or reset their own budget."""
    if not isinstance(policy, WorkerCapacityPolicy) or not isinstance(
        observation, WorkerCapacityObservation
    ):
        fail("assess_worker_capacity", "requires typed policy and observation")
    capacity_alias(worker_id, "assess_worker_capacity.worker_id")
    if job_identity is not None and not isinstance(job_identity, ContentIdentity):
        fail("assess_worker_capacity.job_identity", "must be typed")
    int_value(now_ms, "assess_worker_capacity.now_ms")
    int_value(attempt, "assess_worker_capacity.attempt")
    roles = tuple(item.role for item in policy.roles)
    findings: list[CapacityFinding] = []

    def result(decision: CapacityDecision) -> CapacityAssessment:
        health = max((item.health for item in findings), key=_HEALTH_ORDER.__getitem__)
        return CapacityAssessment(
            worker_id,
            policy.identity,
            observation.identity,
            job_identity,
            now_ms,
            observation.expires_at_ms,
            health,
            decision,
            attempt,
            tuple(findings),
        )

    bindings = (
        (observation.worker_id != worker_id, "worker-mismatch"),
        (observation.policy_identity != policy.identity, "policy-mismatch"),
        (observation.job_identity != job_identity, "job-mismatch"),
        (observation.completed_at_ms > now_ms, "future-observation"),
        (
            observation.completed_at_ms - observation.started_at_ms
            > policy.probe_timeout_ms,
            "probe-window-exceeded",
        ),
        (
            observation.expires_at_ms - observation.started_at_ms
            > policy.maximum_age_ms,
            "expiry-exceeds-policy",
        ),
    )
    for invalid, reason in bindings:
        if invalid:
            findings.append(
                CapacityFinding(
                    roles, "observation", CapacityHealth.UNKNOWN, reason, True
                )
            )
    if findings:
        return result(CapacityDecision.HOLD)
    if now_ms >= observation.expires_at_ms:
        findings.append(
            CapacityFinding(
                roles, "observation", CapacityHealth.UNKNOWN, "stale-observation", True
            )
        )
        return result(
            CapacityDecision.RETRY_AFTER_RECHECK
            if attempt < policy.maximum_retries
            else CapacityDecision.HOLD
        )

    findings.append(
        CapacityFinding(
            roles, "observation", CapacityHealth.HEALTHY, "observation-current", True
        )
    )
    samples = {item.role: item for item in observation.samples}
    if set(samples) - set(roles):
        findings.append(
            CapacityFinding(
                roles, "observation", CapacityHealth.UNKNOWN, "unexpected-role", True
            )
        )
        return result(CapacityDecision.HOLD)
    volumes: dict[str, _Group] = defaultdict(list)
    quotas: dict[str, list[tuple[StorageRoleCapacityPolicy, QuotaCapacitySample]]] = (
        defaultdict(list)
    )
    for item in policy.roles:
        sample = samples.get(item.role)
        if sample is None:
            findings.append(
                CapacityFinding(
                    (item.role,),
                    "observation",
                    CapacityHealth.UNKNOWN,
                    "missing-role",
                    True,
                )
            )
            continue
        if sample.volume is not None:
            volumes[sample.volume].append((item, sample))
        if sample.available_bytes.status is not CapacityProbeStatus.MEASURED:
            findings.append(_unknown(item.role, "bytes", sample.available_bytes, True))
        if sample.available_inodes.status is not CapacityProbeStatus.MEASURED:
            required = item.require_inodes or bool(
                item.additional_inodes or item.reserve_inodes
            )
            if (
                required
                or sample.available_inodes.status
                is not CapacityProbeStatus.NOT_APPLICABLE
            ):
                findings.append(
                    _unknown(item.role, "inodes", sample.available_inodes, required)
                )
        if (
            sample.available_inodes.status is CapacityProbeStatus.NOT_APPLICABLE
            and not (
                item.require_inodes or item.additional_inodes or item.reserve_inodes
            )
        ):
            findings.append(
                CapacityFinding(
                    (item.role,),
                    "inodes",
                    CapacityHealth.HEALTHY,
                    "inodes-not-applicable",
                    False,
                )
            )
        for quota in sample.quotas:
            quotas[quota.domain].append((item, quota))
            for resource, metric, required in (
                ("quota", quota.available_bytes, item.require_quota),
                (
                    "quota-inodes",
                    quota.available_inodes,
                    item.require_quota
                    and bool(
                        item.require_inodes
                        or item.additional_inodes
                        or item.reserve_inodes
                    ),
                ),
            ):
                if metric.status not in (
                    CapacityProbeStatus.MEASURED,
                    CapacityProbeStatus.NOT_APPLICABLE,
                ):
                    findings.append(_unknown(item.role, resource, metric, required))

    for volume in sorted(volumes):
        findings.extend(_volume_findings(volumes[volume], policy))
    for domain in sorted(quotas):
        group = quotas[domain]
        for resource, metric_name, additional_name, reserve_name, warning in (
            (
                "quota",
                "available_bytes",
                "additional_bytes",
                "reserve_bytes",
                policy.warning_headroom_bytes,
            ),
            (
                "quota-inodes",
                "available_inodes",
                "additional_inodes",
                "reserve_inodes",
                0,
            ),
        ):
            measured = [
                getattr(quota, metric_name).value
                for _item, quota in group
                if getattr(quota, metric_name).status is CapacityProbeStatus.MEASURED
            ]
            if not measured:
                if all(
                    getattr(quota, metric_name).status
                    is CapacityProbeStatus.NOT_APPLICABLE
                    for _item, quota in group
                ):
                    findings.append(
                        CapacityFinding(
                            tuple(item.role for item, _quota in group),
                            resource,
                            CapacityHealth.HEALTHY,
                            "quota-not-applicable",
                            False,
                        )
                    )
                continue
            if any(
                getattr(quota, metric_name).status is CapacityProbeStatus.NOT_APPLICABLE
                for _item, quota in group
            ):
                findings.append(
                    CapacityFinding(
                        tuple(item.role for item, _quota in group),
                        resource,
                        CapacityHealth.UNKNOWN,
                        "conflicting-quota-applicability",
                        True,
                    )
                )
            # Include every role bound to this domain, even if one measurement
            # failed. A successful sample cannot erase another role's demand.
            minimum = sum(
                getattr(item, additional_name) for item, _quota in group
            ) + max(getattr(item, reserve_name) for item, _quota in group)
            findings.append(
                _budget(
                    tuple(item.role for item, _quota in group),
                    resource,
                    min(measured),
                    minimum,
                    warning,
                )
            )

    decision = CapacityDecision.PROCEED
    if policy.write_heavy and any(
        item.health is CapacityHealth.CRITICAL for item in findings
    ):
        decision = CapacityDecision.HOLD
    elif any(
        item.health is CapacityHealth.UNKNOWN and item.required for item in findings
    ):
        decision = (
            CapacityDecision.RETRY_AFTER_RECHECK
            if attempt < policy.maximum_retries
            else CapacityDecision.HOLD
        )
    return result(decision)
