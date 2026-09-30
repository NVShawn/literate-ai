"""Bounded alert transitions; reporting history never changes admission decisions."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.application.worker_capacity import CapacityAssessment, CapacityHealth
from literate_ai.contracts import ContentIdentity, WorkerCapacityObservation
from literate_ai.contracts._validation import (
    contract_fields,
    fields,
    int_value,
    list_value,
)
from literate_ai.contracts.worker_capacity import capacity_alias

SCHEMA = "literate-ai/private-worker-alert-history@1"
MAX_METRICS = 80
RETENTION_MS = 24 * 60 * 60 * 1000
_RESOURCES = frozenset(
    {
        "bytes",
        "cpu",
        "gpu",
        "inodes",
        "memory",
        "observation",
        "paging",
        "quota",
        "quota-inodes",
    }
)
_ORDER = {value.value: index for index, value in enumerate(CapacityHealth)}
_ACTIONS = {
    "healthy": "Use fresh assessment; dispatch and cleanup need their own authority.",
    "critical": (
        "Hold new allocations and inspect storage; "
        "cleanup requires exact-target authorization."
    ),
    "unknown": "Check probe support and access, then collect fresh observations.",
    "warning": "Review the remaining reserve before additional allocation.",
}


@dataclass(frozen=True)
class AlertMetric:
    role: str
    resource: str
    health: str
    reasons: tuple[str, ...]

    def __post_init__(self):
        capacity_alias(self.role, "AlertMetric.role")
        if self.resource not in _RESOURCES or self.health not in _ORDER:
            raise ValueError("worker.alert_metric_invalid")
        if (
            not isinstance(self.reasons, tuple)
            or not 1 <= len(self.reasons) <= 16
            or self.reasons != tuple(sorted(set(self.reasons)))
        ):
            raise ValueError("worker.alert_reasons_invalid")
        for reason in self.reasons:
            capacity_alias(reason, "AlertMetric.reason")

    @property
    def key(self):
        return self.role, self.resource

    def to_dict(self):
        return {
            "role": self.role,
            "resource": self.resource,
            "health": self.health,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value):
        data = fields(
            value,
            path="AlertMetric",
            required=frozenset({"role", "resource", "health", "reasons"}),
        )
        return cls(
            data["role"],
            data["resource"],
            data["health"],
            tuple(list_value(data["reasons"], "AlertMetric.reasons")),
        )


@dataclass(frozen=True)
class WorkerAlertHistory:
    worker_id: str
    policy_identity: ContentIdentity
    job_identity: ContentIdentity | None
    observation_identity: ContentIdentity
    started_at_ms: int
    completed_at_ms: int
    recorded_at_ms: int
    metrics: tuple[AlertMetric, ...]

    def __post_init__(self):
        capacity_alias(self.worker_id, "WorkerAlertHistory.worker_id")
        if (
            any(
                not isinstance(v, ContentIdentity)
                for v in (self.policy_identity, self.observation_identity)
            )
            or self.job_identity is not None
            and not isinstance(self.job_identity, ContentIdentity)
        ):
            raise ValueError("worker.alert_identity_invalid")
        for value in (self.started_at_ms, self.completed_at_ms, self.recorded_at_ms):
            int_value(value, "WorkerAlertHistory.time")
        if not self.started_at_ms <= self.completed_at_ms <= self.recorded_at_ms:
            raise ValueError("worker.alert_time_invalid")
        if (
            not isinstance(self.metrics, tuple)
            or not 1 <= len(self.metrics) <= MAX_METRICS
            or any(not isinstance(v, AlertMetric) for v in self.metrics)
        ):
            raise ValueError("worker.alert_metrics_invalid")
        keys = [v.key for v in self.metrics]
        if keys != sorted(set(keys)):
            raise ValueError("worker.alert_metrics_invalid")

    def to_dict(self):
        return {
            "schema": SCHEMA,
            "worker_id": self.worker_id,
            "policy_identity": self.policy_identity.uri,
            "job_identity": None
            if self.job_identity is None
            else self.job_identity.uri,
            "observation_identity": self.observation_identity.uri,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "recorded_at_ms": self.recorded_at_ms,
            "metrics": [v.to_dict() for v in self.metrics],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path="WorkerAlertHistory",
            schema_uri=SCHEMA,
            required=frozenset(cls.__dataclass_fields__),
        )
        return cls(
            data["worker_id"],
            ContentIdentity.parse_uri(data["policy_identity"]),
            None
            if data["job_identity"] is None
            else ContentIdentity.parse_uri(data["job_identity"]),
            ContentIdentity.parse_uri(data["observation_identity"]),
            data["started_at_ms"],
            data["completed_at_ms"],
            data["recorded_at_ms"],
            tuple(
                AlertMetric.from_dict(v)
                for v in list_value(data["metrics"], "WorkerAlertHistory.metrics")
            ),
        )


def alert_transitions(
    assessment, observation, previous=None, *, additional_findings=()
):
    if (
        not isinstance(assessment, CapacityAssessment)
        or not isinstance(observation, WorkerCapacityObservation)
        or assessment.observation_identity != observation.identity
    ):
        raise ValueError("worker.alert_observation_mismatch")
    if (assessment.worker_id, assessment.policy_identity, assessment.job_identity) != (
        observation.worker_id,
        observation.policy_identity,
        observation.job_identity,
    ):
        raise ValueError("worker.alert_observation_mismatch")
    findings = (*assessment.findings, *additional_findings)
    if len(findings) > 1024:
        raise ValueError("worker.alert_findings_limit")
    reset = None
    prior = {}
    if previous is not None:
        if (
            not isinstance(previous, WorkerAlertHistory)
            or previous.worker_id != assessment.worker_id
        ):
            raise ValueError("worker.alert_worker_mismatch")
        if previous.recorded_at_ms > assessment.assessed_at_ms:
            raise ValueError("worker.alert_history_from_future")
        if (previous.policy_identity, previous.job_identity) != (
            assessment.policy_identity,
            assessment.job_identity,
        ):
            reset = "context-changed"
        elif assessment.assessed_at_ms - previous.recorded_at_ms >= RETENTION_MS:
            reset = "history-expired"
        else:
            old_order = previous.started_at_ms, previous.completed_at_ms
            new_order = observation.started_at_ms, observation.completed_at_ms
            if (
                new_order < old_order
                or new_order == old_order
                and previous.observation_identity != observation.identity
            ):
                raise ValueError("worker.alert_observation_out_of_order")
            roles = {role for finding in findings for role in finding.roles}
            if any(metric.role not in roles for metric in previous.metrics):
                raise ValueError("worker.alert_role_mismatch")
            prior = {v.key: v for v in previous.metrics}
    groups = {}
    for finding in findings:
        for role in finding.roles:
            groups.setdefault((role, finding.resource), []).append(finding)
            if len(groups) > MAX_METRICS:
                raise ValueError("worker.alert_metrics_limit")
    # Missing measurements cannot erase a previous incident or announce recovery.
    for key in prior.keys() - groups.keys():
        groups[key] = []
    metrics, events = [], []
    suppressed = 0
    for (role, resource), findings in sorted(groups.items()):
        health = (
            max((f.health.value for f in findings), key=_ORDER.__getitem__)
            if findings
            else "unknown"
        )
        reasons = (
            tuple(sorted({f.reason for f in findings if f.health.value == health}))
            if findings
            else ("no-current-measurement",)
        )
        current = AlertMetric(role, resource, health, reasons)
        metrics.append(current)
        before = prior.get(current.key)
        if (
            before == current
            or before is None
            and health == "healthy"
            or before is not None
            and before.health == health == "healthy"
        ):
            suppressed += health != "healthy"
            continue
        transition = (
            "initial"
            if before is None
            else "recovered"
            if health == "healthy"
            else "changed"
        )
        if before is not None and health == "critical" and before.health != "critical":
            transition = "escalated"
        elif (
            before is not None
            and health != "healthy"
            and "unknown" not in (health, before.health)
        ):
            if _ORDER[health] > _ORDER[before.health]:
                transition = "escalated"
            elif _ORDER[health] < _ORDER[before.health]:
                transition = "improved"
        events.append(
            {
                "schema": "literate-ai/worker-health-event@1",
                "worker_id": assessment.worker_id,
                "role": role,
                "resource": resource,
                "health": health,
                "previous_health": None if before is None else before.health,
                "transition": transition,
                "reasons": list(reasons),
                "policy_identity": assessment.policy_identity.uri,
                "job_identity": None
                if assessment.job_identity is None
                else assessment.job_identity.uri,
                "observation_identity": observation.identity.uri,
                "impact": assessment.decision.value,
                "next_action": _ACTIONS[health],
                "findings": [f.to_dict() for f in findings],
            }
        )
    state = WorkerAlertHistory(
        assessment.worker_id,
        assessment.policy_identity,
        assessment.job_identity,
        observation.identity,
        observation.started_at_ms,
        observation.completed_at_ms,
        assessment.assessed_at_ms,
        tuple(metrics),
    )
    return (
        state,
        events,
        {"recorded": True, "reset_reason": reset, "suppressed": suppressed},
    )
