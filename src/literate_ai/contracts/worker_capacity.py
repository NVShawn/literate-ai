"""Private, time-bounded capacity inputs; never authored hardware requirements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    fields,
    int_value,
    list_value,
    string_value,
)
from .identity import ContentIdentity, canonical_identity

_ALIAS = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
STORAGE_ROLES = frozenset({"workspace", "temp", "cache", "output"})


def capacity_alias(value: Any, path: str) -> str:
    result = string_value(value, path, max_length=64)
    if not _ALIAS.fullmatch(result):
        fail(path, "must be a portable resource alias")
    return result


def _typed_tuple(value: Any, kind: type, path: str, *, minimum: int = 0) -> None:
    if not isinstance(value, tuple) or not minimum <= len(value) <= 16:
        fail(path, f"must be a tuple with {minimum} to 16 entries")
    if any(not isinstance(item, kind) for item in value):
        fail(path, "must contain typed entries")
    aliases = tuple(item.role for item in value)
    if aliases != tuple(sorted(set(aliases))):
        fail(path, "role aliases must be uniquely sorted")


def _role_values(value: Any, path: str, *, minimum: int = 0):
    entries = list_value(value, path)
    if not minimum <= len(entries) <= 16:
        fail(path, f"must contain {minimum} to 16 entries")
    return entries


class CapacityProbeStatus(StrEnum):
    MEASURED = "measured"
    NOT_APPLICABLE = "not-applicable"
    UNKNOWN = "unknown"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    TIMED_OUT = "timed-out"
    UNREACHABLE = "unreachable"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class CapacityMetric:
    status: CapacityProbeStatus
    value: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CapacityProbeStatus):
            fail("CapacityMetric.status", "must be typed")
        if self.status is CapacityProbeStatus.MEASURED:
            int_value(self.value, "CapacityMetric.value")
        elif self.value is not None:
            fail("CapacityMetric.value", "only a measured metric has a value")

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status.value, "value": self.value}

    @classmethod
    def from_dict(cls, value: Any) -> CapacityMetric:
        data = fields(
            value, path="CapacityMetric", required=frozenset({"status", "value"})
        )
        return cls(
            enum_value(CapacityProbeStatus, data["status"], "CapacityMetric.status"),
            data["value"],
        )


@dataclass(frozen=True, slots=True)
class StorageRoleCapacityPolicy:
    role: str
    kind: str
    additional_bytes: int
    reserve_bytes: int
    minimum_free_basis_points: int
    additional_inodes: int
    reserve_inodes: int
    require_quota: bool
    require_inodes: bool

    def __post_init__(self) -> None:
        capacity_alias(self.role, "StorageRoleCapacityPolicy.role")
        string_value(self.kind, "StorageRoleCapacityPolicy.kind", max_length=16)
        if self.kind not in STORAGE_ROLES:
            fail(
                "StorageRoleCapacityPolicy.kind",
                "must be workspace, temp, cache, or output",
            )
        for name in (
            "additional_bytes",
            "reserve_bytes",
            "additional_inodes",
            "reserve_inodes",
        ):
            int_value(getattr(self, name), f"StorageRoleCapacityPolicy.{name}")
        int_value(
            self.minimum_free_basis_points,
            "StorageRoleCapacityPolicy.minimum_free_basis_points",
            maximum=10000,
        )
        bool_value(self.require_quota, "StorageRoleCapacityPolicy.require_quota")
        bool_value(self.require_inodes, "StorageRoleCapacityPolicy.require_inodes")

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Any) -> StorageRoleCapacityPolicy:
        data = fields(
            value,
            path="StorageRoleCapacityPolicy",
            required=frozenset(cls.__dataclass_fields__),
        )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class WorkerCapacityPolicy:
    roles: tuple[StorageRoleCapacityPolicy, ...]
    write_heavy: bool
    maximum_age_ms: int
    probe_timeout_ms: int
    maximum_retries: int
    warning_headroom_bytes: int
    storage_bindings_identity: ContentIdentity

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:worker-capacity-policy"

    def __post_init__(self) -> None:
        if not isinstance(self.storage_bindings_identity, ContentIdentity):
            fail("WorkerCapacityPolicy.storage_bindings_identity", "must be typed")
        _typed_tuple(
            self.roles,
            StorageRoleCapacityPolicy,
            "WorkerCapacityPolicy.roles",
            minimum=4,
        )
        if {item.kind for item in self.roles} != STORAGE_ROLES:
            fail(
                "WorkerCapacityPolicy.roles",
                "must declare workspace, temp, cache, and output roles",
            )
        bool_value(self.write_heavy, "WorkerCapacityPolicy.write_heavy")
        int_value(
            self.maximum_age_ms,
            "WorkerCapacityPolicy.maximum_age_ms",
            minimum=1,
            maximum=3600000,
        )
        int_value(
            self.probe_timeout_ms,
            "WorkerCapacityPolicy.probe_timeout_ms",
            minimum=1,
            maximum=60000,
        )
        int_value(
            self.maximum_retries, "WorkerCapacityPolicy.maximum_retries", maximum=10
        )
        int_value(
            self.warning_headroom_bytes, "WorkerCapacityPolicy.warning_headroom_bytes"
        )
        if self.probe_timeout_ms > self.maximum_age_ms:
            fail(
                "WorkerCapacityPolicy.probe_timeout_ms",
                "cannot exceed the observation lifetime",
            )
        if not self.write_heavy and any(
            item.additional_bytes or item.additional_inodes for item in self.roles
        ):
            fail(
                "WorkerCapacityPolicy.write_heavy",
                "allocating work cannot use read-only admission",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "roles": [item.to_dict() for item in self.roles],
            "write_heavy": self.write_heavy,
            "maximum_age_ms": self.maximum_age_ms,
            "probe_timeout_ms": self.probe_timeout_ms,
            "maximum_retries": self.maximum_retries,
            "warning_headroom_bytes": self.warning_headroom_bytes,
            "storage_bindings_identity": self.storage_bindings_identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> WorkerCapacityPolicy:
        data = contract_fields(
            value,
            path="WorkerCapacityPolicy",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "roles",
                    "write_heavy",
                    "maximum_age_ms",
                    "probe_timeout_ms",
                    "maximum_retries",
                    "warning_headroom_bytes",
                    "storage_bindings_identity",
                }
            ),
        )
        return cls(
            tuple(
                StorageRoleCapacityPolicy.from_dict(item)
                for item in _role_values(
                    data["roles"], "WorkerCapacityPolicy.roles", minimum=4
                )
            ),
            data["write_heavy"],
            data["maximum_age_ms"],
            data["probe_timeout_ms"],
            data["maximum_retries"],
            data["warning_headroom_bytes"],
            ContentIdentity.from_dict(data["storage_bindings_identity"]),
        )


@dataclass(frozen=True, slots=True)
class QuotaCapacitySample:
    domain: str
    available_bytes: CapacityMetric
    available_inodes: CapacityMetric

    def __post_init__(self) -> None:
        capacity_alias(self.domain, "QuotaCapacitySample.domain")
        for name in ("available_bytes", "available_inodes"):
            if not isinstance(getattr(self, name), CapacityMetric):
                fail(f"QuotaCapacitySample.{name}", "must be typed")

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "available_bytes": self.available_bytes.to_dict(),
            "available_inodes": self.available_inodes.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> QuotaCapacitySample:
        data = fields(
            value,
            path="QuotaCapacitySample",
            required=frozenset(cls.__dataclass_fields__),
        )
        return cls(
            data["domain"],
            CapacityMetric.from_dict(data["available_bytes"]),
            CapacityMetric.from_dict(data["available_inodes"]),
        )


@dataclass(frozen=True, slots=True)
class StorageCapacitySample:
    role: str
    volume: str | None
    total_bytes: int | None
    available_bytes: CapacityMetric
    available_inodes: CapacityMetric
    quotas: tuple[QuotaCapacitySample, ...]

    def __post_init__(self) -> None:
        capacity_alias(self.role, "StorageCapacitySample.role")
        if self.volume is not None:
            capacity_alias(self.volume, "StorageCapacitySample.volume")
        for name in ("available_bytes", "available_inodes"):
            if not isinstance(getattr(self, name), CapacityMetric):
                fail(f"StorageCapacitySample.{name}", "must be typed")
        if self.total_bytes is not None:
            int_value(self.total_bytes, "StorageCapacitySample.total_bytes", minimum=1)
        if self.available_bytes.status is CapacityProbeStatus.MEASURED:
            if self.volume is None or self.total_bytes is None:
                fail(
                    "StorageCapacitySample",
                    "measured bytes require a volume and total capacity",
                )
            assert self.available_bytes.value is not None
            if self.available_bytes.value > self.total_bytes:
                fail(
                    "StorageCapacitySample.available_bytes",
                    "cannot exceed total capacity",
                )
        if (
            self.available_inodes.status is CapacityProbeStatus.MEASURED
            and self.volume is None
        ):
            fail("StorageCapacitySample.volume", "measured inodes require a volume")
        if (
            not isinstance(self.quotas, tuple)
            or not 1 <= len(self.quotas) <= 8
            or any(not isinstance(q, QuotaCapacitySample) for q in self.quotas)
        ):
            fail("StorageCapacitySample.quotas", "requires one to eight typed domains")
        domains = [q.domain for q in self.quotas]
        if domains != sorted(set(domains)):
            fail("StorageCapacitySample.quotas", "requires unique sorted domains")

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "volume": self.volume,
            "total_bytes": self.total_bytes,
            "available_bytes": self.available_bytes.to_dict(),
            "available_inodes": self.available_inodes.to_dict(),
            "quotas": [q.to_dict() for q in self.quotas],
        }

    @classmethod
    def from_dict(cls, value: Any) -> StorageCapacitySample:
        data = fields(
            value,
            path="StorageCapacitySample",
            required=frozenset(cls.__dataclass_fields__),
        )
        return cls(
            data["role"],
            data["volume"],
            data["total_bytes"],
            CapacityMetric.from_dict(data["available_bytes"]),
            CapacityMetric.from_dict(data["available_inodes"]),
            tuple(
                QuotaCapacitySample.from_dict(q)
                for q in list_value(data["quotas"], "StorageCapacitySample.quotas")
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkerCapacityObservation:
    worker_id: str
    policy_identity: ContentIdentity
    job_identity: ContentIdentity | None
    os_family: str
    started_at_ms: int
    completed_at_ms: int
    expires_at_ms: int
    samples: tuple[StorageCapacitySample, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:worker-capacity-observation"

    def __post_init__(self) -> None:
        capacity_alias(self.worker_id, "WorkerCapacityObservation.worker_id")
        if not isinstance(self.policy_identity, ContentIdentity):
            fail("WorkerCapacityObservation.policy_identity", "must be typed")
        if self.job_identity is not None and not isinstance(
            self.job_identity, ContentIdentity
        ):
            fail("WorkerCapacityObservation.job_identity", "must be typed")
        string_value(
            self.os_family, "WorkerCapacityObservation.os_family", max_length=16
        )
        if self.os_family not in {"linux", "macos", "windows"}:
            fail(
                "WorkerCapacityObservation.os_family",
                "must be linux, macos, or windows",
            )
        for name in ("started_at_ms", "completed_at_ms", "expires_at_ms"):
            int_value(getattr(self, name), f"WorkerCapacityObservation.{name}")
        if (
            self.completed_at_ms < self.started_at_ms
            or self.expires_at_ms <= self.started_at_ms
        ):
            fail(
                "WorkerCapacityObservation",
                "requires an ordered sample window and positive lifetime",
            )
        _typed_tuple(
            self.samples, StorageCapacitySample, "WorkerCapacityObservation.samples"
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "worker_id": self.worker_id,
            "policy_identity": self.policy_identity.to_dict(),
            "job_identity": None
            if self.job_identity is None
            else self.job_identity.to_dict(),
            "os_family": self.os_family,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "samples": [item.to_dict() for item in self.samples],
        }

    @classmethod
    def from_dict(cls, value: Any) -> WorkerCapacityObservation:
        data = contract_fields(
            value,
            path="WorkerCapacityObservation",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "worker_id",
                    "policy_identity",
                    "job_identity",
                    "os_family",
                    "started_at_ms",
                    "completed_at_ms",
                    "expires_at_ms",
                    "samples",
                }
            ),
        )
        return cls(
            data["worker_id"],
            ContentIdentity.from_dict(data["policy_identity"]),
            None
            if data["job_identity"] is None
            else ContentIdentity.from_dict(data["job_identity"]),
            data["os_family"],
            data["started_at_ms"],
            data["completed_at_ms"],
            data["expires_at_ms"],
            tuple(
                StorageCapacitySample.from_dict(item)
                for item in _role_values(
                    data["samples"], "WorkerCapacityObservation.samples"
                )
            ),
        )
