"""Fail-closed security decisions over exact immutable identities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Any, ClassVar, Final, Protocol

_SHA256_URI = re.compile(r"^sha256:[0-9a-f]{64}$")

SECURITY_ORIGIN_ATTESTATION_SCHEMA: Final = (
    "urn:literate-ai:schema:v2:security-origin-attestation"
)
SECURITY_FINDING_SCHEMA: Final = "urn:literate-ai:schema:v2:security-finding"
SECURITY_CLASSIFICATION_SCHEMA: Final = (
    "urn:literate-ai:schema:v2:security-classification"
)
BUILD_REQUEST_SCHEMA: Final = "urn:literate-ai:schema:v2:build-request"
BUILD_REQUEST_DECLARATION_SCHEMA: Final = (
    "urn:literate-ai:schema:v2:build-request-declaration"
)
OBSERVATION_REQUEST_SCHEMA: Final = "urn:literate-ai:schema:v2:observation-request"
BUILD_AUTHORIZATION_SCHEMA: Final = "urn:literate-ai:schema:v2:build-authorization"
OBSERVATION_EXECUTION_AUTHORIZATION_SCHEMA: Final = (
    "urn:literate-ai:schema:v2:observation-execution-authorization"
)
SECURITY_SCAN_REPORT_SCHEMA: Final = "urn:literate-ai:schema:v2:security-scan-report"
AUTHORIZATION_REVOCATION_SET_SCHEMA: Final = (
    "urn:literate-ai:schema:v2:authorization-revocation-set"
)

_SECURITY_FIELDS: Final[dict[str, frozenset[str]]] = {
    SECURITY_ORIGIN_ATTESTATION_SCHEMA: frozenset(
        {
            "source_digest",
            "signer",
            "trust_root",
            "signature_identity",
            "verified",
            "revoked",
        }
    ),
    SECURITY_FINDING_SCHEMA: frozenset(
        {
            "finding_id",
            "source_digest",
            "category",
            "severity",
            "scanner_identity",
            "message",
        }
    ),
    SECURITY_CLASSIFICATION_SCHEMA: frozenset(
        {
            "effective_revision_digest",
            "source_digests",
            "dependency_classification_digests",
            "origin_attestation_digests",
            "finding_ids",
            "policy_digest",
            "profile",
            "maximum_severity",
            "permitted_privileges",
            "decision_reason",
        }
    ),
    BUILD_REQUEST_SCHEMA: frozenset(
        {
            "effective_revision_digest",
            "source_bundle_digest",
            "builder_id",
            "toolchain_digest",
            "sandbox_profile",
            "requested_privileges",
            "allowed_outputs",
        }
    ),
    BUILD_REQUEST_DECLARATION_SCHEMA: frozenset(
        {
            "effective_revision_digest",
            "builder_id",
            "toolchain_digest",
            "sandbox_profile",
            "requested_privileges",
            "allowed_outputs",
        }
    ),
    OBSERVATION_REQUEST_SCHEMA: frozenset(
        {
            "effective_revision_digest",
            "source_digests",
            "runner_id",
            "harness_digest",
            "sandbox_profile",
            "requested_privileges",
            "allowed_outputs",
        }
    ),
    BUILD_AUTHORIZATION_SCHEMA: frozenset(
        {
            "authorization_id",
            "classification_digest",
            "request_digest",
            "effective_revision_digest",
            "actor",
            "reason",
            "profile",
            "privileges",
            "issued_at",
            "expires_at",
            "warning",
            "revoked",
        }
    ),
    OBSERVATION_EXECUTION_AUTHORIZATION_SCHEMA: frozenset(
        {
            "authorization_id",
            "classification_digest",
            "request_digest",
            "effective_revision_digest",
            "actor",
            "reason",
            "profile",
            "privileges",
            "issued_at",
            "expires_at",
            "warning",
            "revoked",
        }
    ),
    SECURITY_SCAN_REPORT_SCHEMA: frozenset(
        {"scanner_identity", "module_digests", "rule_set_digest", "findings"}
    ),
    AUTHORIZATION_REVOCATION_SET_SCHEMA: frozenset({"entries"}),
}


def adapt_unreleased_post_v011_security_document(
    value: Mapping[str, object], *, expected_schema: str | None = None
) -> dict[str, object]:
    """Add a v2 envelope to one exact historical security field set.

    This helper identifies an envelope only.  Use the matching typed ``from_dict``
    reader to validate values and obtain a canonical current document.
    """

    if "schema" in value:
        raise ValueError("security legacy adapter does not accept versioned input")
    matches = [
        schema for schema, fields in _SECURITY_FIELDS.items() if set(value) == fields
    ]
    if expected_schema is not None:
        if expected_schema not in matches:
            raise ValueError("security fields do not match the expected legacy shape")
        matches = [expected_schema]
    if len(matches) != 1:
        raise ValueError("security fields do not identify one supported legacy shape")
    return {"schema": matches[0], **dict(value)}


def normalize_security_document(
    value: Mapping[str, object], *, expected_schema: str | None = None
) -> dict[str, object]:
    """Normalize only a security envelope; typed readers validate its values."""

    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("security document must be an object")
    if "schema" not in value:
        return adapt_unreleased_post_v011_security_document(
            value, expected_schema=expected_schema
        )
    schema = value["schema"]
    if not isinstance(schema, str) or schema not in _SECURITY_FIELDS:
        raise ValueError(f"security schema is unsupported: {schema!r}")
    if expected_schema is not None and schema != expected_schema:
        raise ValueError(f"expected {expected_schema}, got {schema!r}")
    if set(value) != _SECURITY_FIELDS[schema] | {"schema"}:
        raise ValueError("security fields do not match the current schema")
    return dict(value)


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _require_digest(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_URI.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase sha256 digest")


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")


def _string(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    if not allow_empty:
        _require_text(value, field_name)
    return value


def _require_unique_texts(
    values: tuple[str, ...], field_name: str, *, required: bool = False
) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    if required and not values:
        raise ValueError(f"{field_name} cannot be empty")
    for value in values:
        _require_text(value, field_name)
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique values")


def _require_unique_digests(
    values: tuple[str, ...], field_name: str, *, required: bool = False
) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    if required and not values:
        raise ValueError(f"{field_name} cannot be empty")
    for value in values:
        _require_digest(value, field_name)
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique values")


def _tuple(value: object, field_name: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array")
    return tuple(value)


def _bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _exact_fields(
    value: Mapping[str, object], fields: frozenset[str], label: str
) -> None:
    if set(value) != fields:
        raise ValueError(f"{label} fields do not match the public schema")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("security timestamps must be timezone-aware")
    return value.astimezone(UTC)


class SecurityProfile(StrEnum):
    CONSTRAINED = "constrained"
    REVIEWED = "reviewed"
    PRIVILEGED_REVIEW = "privileged-review"
    BLOCKED = "blocked"
    YOLO = "yolo"


class FindingSeverity(IntEnum):
    INFORMATIONAL = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True, slots=True)
class OriginAttestation:
    SCHEMA: ClassVar[str] = SECURITY_ORIGIN_ATTESTATION_SCHEMA

    source_digest: str
    signer: str
    trust_root: str
    signature_identity: str
    verified: bool
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_digest(self.source_digest, "source_digest")
        _require_text(self.signer, "signer")
        _require_text(self.trust_root, "trust_root")
        _require_text(self.signature_identity, "signature_identity")
        _bool(self.verified, "verified")
        _bool(self.revoked, "revoked")

    @property
    def accepted(self) -> bool:
        return self.verified and not self.revoked

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> OriginAttestation:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        _exact_fields(
            value,
            frozenset(
                {
                    "schema",
                    "source_digest",
                    "signer",
                    "trust_root",
                    "signature_identity",
                    "verified",
                    "revoked",
                }
            ),
            "origin attestation",
        )
        return cls(
            source_digest=_string(value["source_digest"], "source_digest"),
            signer=_string(value["signer"], "signer"),
            trust_root=_string(value["trust_root"], "trust_root"),
            signature_identity=_string(
                value["signature_identity"], "signature_identity"
            ),
            verified=_bool(value["verified"], "verified"),
            revoked=_bool(value["revoked"], "revoked"),
        )


@dataclass(frozen=True, slots=True)
class SecurityFinding:
    SCHEMA: ClassVar[str] = SECURITY_FINDING_SCHEMA

    finding_id: str
    source_digest: str
    category: str
    severity: FindingSeverity
    scanner_identity: str
    message: str

    def __post_init__(self) -> None:
        _require_digest(self.source_digest, "source_digest")
        _require_text(self.finding_id, "finding_id")
        _require_text(self.category, "category")
        _require_text(self.scanner_identity, "scanner_identity")
        if not isinstance(self.severity, FindingSeverity):
            raise ValueError("severity must be a FindingSeverity")
        if not isinstance(self.message, str):
            raise ValueError("message must be text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "finding_id": self.finding_id,
            "source_digest": self.source_digest,
            "category": self.category,
            "severity": int(self.severity),
            "scanner_identity": self.scanner_identity,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SecurityFinding:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        _exact_fields(
            value,
            frozenset(
                {
                    "schema",
                    "finding_id",
                    "source_digest",
                    "category",
                    "severity",
                    "scanner_identity",
                    "message",
                }
            ),
            "security finding",
        )
        return cls(
            finding_id=_string(value["finding_id"], "finding_id"),
            source_digest=_string(value["source_digest"], "source_digest"),
            category=_string(value["category"], "category"),
            severity=FindingSeverity(_integer(value["severity"], "severity")),
            scanner_identity=_string(value["scanner_identity"], "scanner_identity"),
            message=_string(value["message"], "message", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class SecurityClassification:
    SCHEMA: ClassVar[str] = SECURITY_CLASSIFICATION_SCHEMA

    effective_revision_digest: str
    source_digests: tuple[str, ...]
    dependency_classification_digests: tuple[str, ...]
    origin_attestation_digests: tuple[str, ...]
    finding_ids: tuple[str, ...]
    policy_digest: str
    profile: SecurityProfile
    maximum_severity: FindingSeverity
    permitted_privileges: tuple[str, ...] = ()
    decision_reason: str = ""

    def __post_init__(self) -> None:
        _require_digest(self.effective_revision_digest, "effective_revision_digest")
        _require_digest(self.policy_digest, "policy_digest")
        _require_unique_digests(self.source_digests, "source_digests", required=True)
        _require_unique_digests(
            self.dependency_classification_digests,
            "dependency_classification_digests",
        )
        _require_unique_digests(
            self.origin_attestation_digests,
            "origin_attestation_digests",
            required=True,
        )
        _require_unique_texts(self.finding_ids, "finding_ids")
        _require_unique_texts(self.permitted_privileges, "permitted_privileges")
        if not isinstance(self.profile, SecurityProfile):
            raise ValueError("profile must be a SecurityProfile")
        if not isinstance(self.maximum_severity, FindingSeverity):
            raise ValueError("maximum_severity must be a FindingSeverity")
        if not isinstance(self.decision_reason, str):
            raise ValueError("decision_reason must be text")

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "effective_revision_digest": self.effective_revision_digest,
            "source_digests": list(self.source_digests),
            "dependency_classification_digests": list(
                self.dependency_classification_digests
            ),
            "origin_attestation_digests": list(self.origin_attestation_digests),
            "finding_ids": list(self.finding_ids),
            "policy_digest": self.policy_digest,
            "profile": self.profile.value,
            "maximum_severity": int(self.maximum_severity),
            "permitted_privileges": list(self.permitted_privileges),
            "decision_reason": self.decision_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SecurityClassification:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        fields = frozenset(
            {
                "schema",
                "effective_revision_digest",
                "source_digests",
                "dependency_classification_digests",
                "origin_attestation_digests",
                "finding_ids",
                "policy_digest",
                "profile",
                "maximum_severity",
                "permitted_privileges",
                "decision_reason",
            }
        )
        _exact_fields(value, fields, "security classification")
        return cls(
            effective_revision_digest=_string(
                value["effective_revision_digest"], "effective_revision_digest"
            ),
            source_digests=tuple(
                _string(item, "source_digests")
                for item in _tuple(value["source_digests"], "source_digests")
            ),
            dependency_classification_digests=tuple(
                _string(item, "dependency_classification_digests")
                for item in _tuple(
                    value["dependency_classification_digests"],
                    "dependency_classification_digests",
                )
            ),
            origin_attestation_digests=tuple(
                _string(item, "origin_attestation_digests")
                for item in _tuple(
                    value["origin_attestation_digests"],
                    "origin_attestation_digests",
                )
            ),
            finding_ids=tuple(
                _string(item, "finding_ids")
                for item in _tuple(value["finding_ids"], "finding_ids")
            ),
            policy_digest=_string(value["policy_digest"], "policy_digest"),
            profile=SecurityProfile(_string(value["profile"], "profile")),
            maximum_severity=FindingSeverity(
                _integer(value["maximum_severity"], "maximum_severity")
            ),
            permitted_privileges=tuple(
                _string(item, "permitted_privileges")
                for item in _tuple(
                    value["permitted_privileges"], "permitted_privileges"
                )
            ),
            decision_reason=_string(
                value["decision_reason"], "decision_reason", allow_empty=True
            ),
        )


@dataclass(frozen=True, slots=True)
class BuildRequestDeclaration:
    """Exact build authority known before generated source bytes exist."""

    SCHEMA: ClassVar[str] = BUILD_REQUEST_DECLARATION_SCHEMA

    effective_revision_digest: str
    builder_id: str
    toolchain_digest: str
    sandbox_profile: str
    requested_privileges: tuple[str, ...]
    allowed_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_digest(self.effective_revision_digest, "effective_revision_digest")
        _require_digest(self.toolchain_digest, "toolchain_digest")
        _require_text(self.builder_id, "builder_id")
        _require_text(self.sandbox_profile, "sandbox_profile")
        _require_unique_texts(self.requested_privileges, "requested_privileges")
        _require_unique_texts(self.allowed_outputs, "allowed_outputs", required=True)

    @property
    def identity(self) -> str:
        return _canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "effective_revision_digest": self.effective_revision_digest,
            "builder_id": self.builder_id,
            "toolchain_digest": self.toolchain_digest,
            "sandbox_profile": self.sandbox_profile,
            "requested_privileges": list(self.requested_privileges),
            "allowed_outputs": list(self.allowed_outputs),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BuildRequestDeclaration:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        return cls(
            effective_revision_digest=_string(
                value["effective_revision_digest"], "effective_revision_digest"
            ),
            builder_id=_string(value["builder_id"], "builder_id"),
            toolchain_digest=_string(value["toolchain_digest"], "toolchain_digest"),
            sandbox_profile=_string(value["sandbox_profile"], "sandbox_profile"),
            requested_privileges=tuple(
                _string(item, "requested_privileges")
                for item in _tuple(
                    value["requested_privileges"], "requested_privileges"
                )
            ),
            allowed_outputs=tuple(
                _string(item, "allowed_outputs")
                for item in _tuple(value["allowed_outputs"], "allowed_outputs")
            ),
        )

    def realize(self, source_bundle_digest: str) -> BuildRequest:
        return BuildRequest(
            effective_revision_digest=self.effective_revision_digest,
            source_bundle_digest=source_bundle_digest,
            builder_id=self.builder_id,
            toolchain_digest=self.toolchain_digest,
            sandbox_profile=self.sandbox_profile,
            requested_privileges=self.requested_privileges,
            allowed_outputs=self.allowed_outputs,
        )


@dataclass(frozen=True, slots=True)
class BuildRequest:
    SCHEMA: ClassVar[str] = BUILD_REQUEST_SCHEMA

    effective_revision_digest: str
    source_bundle_digest: str
    builder_id: str
    toolchain_digest: str
    sandbox_profile: str
    requested_privileges: tuple[str, ...]
    allowed_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_digest(self.effective_revision_digest, "effective_revision_digest")
        _require_digest(self.source_bundle_digest, "source_bundle_digest")
        _require_digest(self.toolchain_digest, "toolchain_digest")
        _require_text(self.builder_id, "builder_id")
        _require_text(self.sandbox_profile, "sandbox_profile")
        _require_unique_texts(self.requested_privileges, "requested_privileges")
        _require_unique_texts(self.allowed_outputs, "allowed_outputs", required=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "effective_revision_digest": self.effective_revision_digest,
            "source_bundle_digest": self.source_bundle_digest,
            "builder_id": self.builder_id,
            "toolchain_digest": self.toolchain_digest,
            "sandbox_profile": self.sandbox_profile,
            "requested_privileges": list(self.requested_privileges),
            "allowed_outputs": list(self.allowed_outputs),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BuildRequest:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        fields = frozenset(
            {
                "schema",
                "effective_revision_digest",
                "source_bundle_digest",
                "builder_id",
                "toolchain_digest",
                "sandbox_profile",
                "requested_privileges",
                "allowed_outputs",
            }
        )
        _exact_fields(value, fields, "build request")
        return cls(
            effective_revision_digest=_string(
                value["effective_revision_digest"], "effective_revision_digest"
            ),
            source_bundle_digest=_string(
                value["source_bundle_digest"], "source_bundle_digest"
            ),
            builder_id=_string(value["builder_id"], "builder_id"),
            toolchain_digest=_string(value["toolchain_digest"], "toolchain_digest"),
            sandbox_profile=_string(value["sandbox_profile"], "sandbox_profile"),
            requested_privileges=tuple(
                _string(item, "requested_privileges")
                for item in _tuple(
                    value["requested_privileges"], "requested_privileges"
                )
            ),
            allowed_outputs=tuple(
                _string(item, "allowed_outputs")
                for item in _tuple(value["allowed_outputs"], "allowed_outputs")
            ),
        )


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    SCHEMA: ClassVar[str] = OBSERVATION_REQUEST_SCHEMA

    effective_revision_digest: str
    source_digests: tuple[str, ...]
    runner_id: str
    harness_digest: str
    sandbox_profile: str
    requested_privileges: tuple[str, ...]
    allowed_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_digest(self.effective_revision_digest, "effective_revision_digest")
        _require_digest(self.harness_digest, "harness_digest")
        _require_unique_digests(self.source_digests, "source_digests", required=True)
        _require_text(self.runner_id, "runner_id")
        _require_text(self.sandbox_profile, "sandbox_profile")
        _require_unique_texts(self.requested_privileges, "requested_privileges")
        _require_unique_texts(self.allowed_outputs, "allowed_outputs", required=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "effective_revision_digest": self.effective_revision_digest,
            "source_digests": list(self.source_digests),
            "runner_id": self.runner_id,
            "harness_digest": self.harness_digest,
            "sandbox_profile": self.sandbox_profile,
            "requested_privileges": list(self.requested_privileges),
            "allowed_outputs": list(self.allowed_outputs),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ObservationRequest:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        fields = frozenset(
            {
                "schema",
                "effective_revision_digest",
                "source_digests",
                "runner_id",
                "harness_digest",
                "sandbox_profile",
                "requested_privileges",
                "allowed_outputs",
            }
        )
        _exact_fields(value, fields, "observation request")
        return cls(
            effective_revision_digest=_string(
                value["effective_revision_digest"], "effective_revision_digest"
            ),
            source_digests=tuple(
                _string(item, "source_digests")
                for item in _tuple(value["source_digests"], "source_digests")
            ),
            runner_id=_string(value["runner_id"], "runner_id"),
            harness_digest=_string(value["harness_digest"], "harness_digest"),
            sandbox_profile=_string(value["sandbox_profile"], "sandbox_profile"),
            requested_privileges=tuple(
                _string(item, "requested_privileges")
                for item in _tuple(
                    value["requested_privileges"], "requested_privileges"
                )
            ),
            allowed_outputs=tuple(
                _string(item, "allowed_outputs")
                for item in _tuple(value["allowed_outputs"], "allowed_outputs")
            ),
        )


@dataclass(frozen=True, slots=True)
class BuildAuthorization:
    SCHEMA: ClassVar[str] = BUILD_AUTHORIZATION_SCHEMA

    authorization_id: str
    classification_digest: str
    request_digest: str
    effective_revision_digest: str
    actor: str
    reason: str
    profile: SecurityProfile
    privileges: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    warning: str | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_digest(self.classification_digest, "classification_digest")
        _require_digest(self.request_digest, "request_digest")
        _require_digest(self.effective_revision_digest, "effective_revision_digest")
        _require_text(self.authorization_id, "authorization_id")
        _require_text(self.actor, "actor")
        _require_text(self.reason, "reason")
        if not isinstance(self.profile, SecurityProfile):
            raise ValueError("profile must be a SecurityProfile")
        _require_unique_texts(self.privileges, "privileges")
        if self.warning is not None and not isinstance(self.warning, str):
            raise ValueError("warning must be text or null")
        _bool(self.revoked, "revoked")
        if _utc(self.expires_at) <= _utc(self.issued_at):
            raise ValueError("authorization expiration must follow issuance")

    def require_valid(self, request: BuildRequest, *, now: datetime) -> None:
        if self.revoked:
            raise AuthorizationError("security.authorization_revoked")
        current = _utc(now)
        if current < _utc(self.issued_at):
            raise AuthorizationError("security.authorization_not_yet_valid")
        if current >= _utc(self.expires_at):
            raise AuthorizationError("security.authorization_expired")
        if self.effective_revision_digest != request.effective_revision_digest:
            raise AuthorizationError("security.authorization_revision_mismatch")
        if self.request_digest != _canonical_digest(request.to_dict()):
            raise AuthorizationError("security.authorization_request_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "authorization_id": self.authorization_id,
            "classification_digest": self.classification_digest,
            "request_digest": self.request_digest,
            "effective_revision_digest": self.effective_revision_digest,
            "actor": self.actor,
            "reason": self.reason,
            "profile": self.profile.value,
            "privileges": list(self.privileges),
            "issued_at": _utc(self.issued_at).isoformat(),
            "expires_at": _utc(self.expires_at).isoformat(),
            "warning": self.warning,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BuildAuthorization:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        fields = frozenset(
            {
                "schema",
                "authorization_id",
                "classification_digest",
                "request_digest",
                "effective_revision_digest",
                "actor",
                "reason",
                "profile",
                "privileges",
                "issued_at",
                "expires_at",
                "warning",
                "revoked",
            }
        )
        _exact_fields(value, fields, "build authorization")
        warning = value["warning"]
        if warning is not None and not isinstance(warning, str):
            raise ValueError("warning must be text or null")
        return cls(
            authorization_id=_string(value["authorization_id"], "authorization_id"),
            classification_digest=_string(
                value["classification_digest"], "classification_digest"
            ),
            request_digest=_string(value["request_digest"], "request_digest"),
            effective_revision_digest=_string(
                value["effective_revision_digest"], "effective_revision_digest"
            ),
            actor=_string(value["actor"], "actor"),
            reason=_string(value["reason"], "reason"),
            profile=SecurityProfile(_string(value["profile"], "profile")),
            privileges=tuple(
                _string(item, "privileges")
                for item in _tuple(value["privileges"], "privileges")
            ),
            issued_at=datetime.fromisoformat(_string(value["issued_at"], "issued_at")),
            expires_at=datetime.fromisoformat(
                _string(value["expires_at"], "expires_at")
            ),
            warning=warning,
            revoked=_bool(value["revoked"], "revoked"),
        )


@dataclass(frozen=True, slots=True)
class ObservationExecutionAuthorization:
    SCHEMA: ClassVar[str] = OBSERVATION_EXECUTION_AUTHORIZATION_SCHEMA

    authorization_id: str
    classification_digest: str
    request_digest: str
    effective_revision_digest: str
    actor: str
    reason: str
    profile: SecurityProfile
    privileges: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    warning: str | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_digest(self.classification_digest, "classification_digest")
        _require_digest(self.request_digest, "request_digest")
        _require_digest(self.effective_revision_digest, "effective_revision_digest")
        _require_text(self.authorization_id, "authorization_id")
        _require_text(self.actor, "actor")
        _require_text(self.reason, "reason")
        if not isinstance(self.profile, SecurityProfile):
            raise ValueError("profile must be a SecurityProfile")
        _require_unique_texts(self.privileges, "privileges")
        if self.warning is not None and not isinstance(self.warning, str):
            raise ValueError("warning must be text or null")
        _bool(self.revoked, "revoked")
        if _utc(self.expires_at) <= _utc(self.issued_at):
            raise ValueError("authorization expiration must follow issuance")

    def require_valid(self, request: ObservationRequest, *, now: datetime) -> None:
        if self.revoked:
            raise AuthorizationError("security.observation_authorization_revoked")
        current = _utc(now)
        if current < _utc(self.issued_at):
            raise AuthorizationError("security.observation_authorization_not_yet_valid")
        if current >= _utc(self.expires_at):
            raise AuthorizationError("security.observation_authorization_expired")
        if self.effective_revision_digest != request.effective_revision_digest:
            raise AuthorizationError(
                "security.observation_authorization_revision_mismatch"
            )
        if self.request_digest != _canonical_digest(request.to_dict()):
            raise AuthorizationError("security.observation_authorization_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "authorization_id": self.authorization_id,
            "classification_digest": self.classification_digest,
            "request_digest": self.request_digest,
            "effective_revision_digest": self.effective_revision_digest,
            "actor": self.actor,
            "reason": self.reason,
            "profile": self.profile.value,
            "privileges": list(self.privileges),
            "issued_at": _utc(self.issued_at).isoformat(),
            "expires_at": _utc(self.expires_at).isoformat(),
            "warning": self.warning,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> ObservationExecutionAuthorization:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        fields = frozenset(
            {
                "schema",
                "authorization_id",
                "classification_digest",
                "request_digest",
                "effective_revision_digest",
                "actor",
                "reason",
                "profile",
                "privileges",
                "issued_at",
                "expires_at",
                "warning",
                "revoked",
            }
        )
        _exact_fields(value, fields, "observation authorization")
        warning = value["warning"]
        if warning is not None and not isinstance(warning, str):
            raise ValueError("warning must be text or null")
        return cls(
            authorization_id=_string(value["authorization_id"], "authorization_id"),
            classification_digest=_string(
                value["classification_digest"], "classification_digest"
            ),
            request_digest=_string(value["request_digest"], "request_digest"),
            effective_revision_digest=_string(
                value["effective_revision_digest"], "effective_revision_digest"
            ),
            actor=_string(value["actor"], "actor"),
            reason=_string(value["reason"], "reason"),
            profile=SecurityProfile(_string(value["profile"], "profile")),
            privileges=tuple(
                _string(item, "privileges")
                for item in _tuple(value["privileges"], "privileges")
            ),
            issued_at=datetime.fromisoformat(_string(value["issued_at"], "issued_at")),
            expires_at=datetime.fromisoformat(
                _string(value["expires_at"], "expires_at")
            ),
            warning=warning,
            revoked=_bool(value["revoked"], "revoked"),
        )


class AuthorizationError(RuntimeError):
    """Stable fail-closed authorization error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BuildAuthorizationVerifier(Protocol):
    """Verify a build grant, including external revocation state when available."""

    def require_build_valid(
        self,
        authorization: BuildAuthorization,
        request: BuildRequest,
        *,
        now: datetime,
    ) -> None: ...


class ObservationExecutionAuthorizationVerifier(Protocol):
    """Verify an observation grant against current external revocation state."""

    def require_observation_valid(
        self,
        authorization: ObservationExecutionAuthorization,
        request: ObservationRequest,
        *,
        now: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DirectBuildAuthorizationVerifier:
    """Snapshot-only verifier for isolated tests; never use as a shipped default."""

    def require_build_valid(
        self,
        authorization: BuildAuthorization,
        request: BuildRequest,
        *,
        now: datetime,
    ) -> None:
        authorization.require_valid(request, now=now)


@dataclass(frozen=True, slots=True)
class FailClosedBuildAuthorizationVerifier:
    """Reject builds until composition supplies a live revocation verifier."""

    def require_build_valid(
        self,
        authorization: BuildAuthorization,
        request: BuildRequest,
        *,
        now: datetime,
    ) -> None:
        del authorization, request, now
        raise AuthorizationError("security.live_revocation_verifier_required")


@dataclass(frozen=True, slots=True)
class LiveBuildAuthorizationVerifier:
    """Read current revocation state at every build authorization boundary."""

    provider: Callable[[], BuildAuthorizationVerifier]

    def require_build_valid(
        self,
        authorization: BuildAuthorization,
        request: BuildRequest,
        *,
        now: datetime,
    ) -> None:
        verifier = self.provider()
        if verifier is self or isinstance(verifier, DirectBuildAuthorizationVerifier):
            raise AuthorizationError("security.live_revocation_verifier_invalid")
        verifier.require_build_valid(authorization, request, now=now)


@dataclass(frozen=True, slots=True)
class DirectObservationExecutionAuthorizationVerifier:
    """Snapshot-only observation verifier for isolated tests."""

    def require_observation_valid(
        self,
        authorization: ObservationExecutionAuthorization,
        request: ObservationRequest,
        *,
        now: datetime,
    ) -> None:
        authorization.require_valid(request, now=now)


@dataclass(frozen=True, slots=True)
class FailClosedObservationExecutionAuthorizationVerifier:
    """Reject observation until composition supplies live revocation state."""

    def require_observation_valid(
        self,
        authorization: ObservationExecutionAuthorization,
        request: ObservationRequest,
        *,
        now: datetime,
    ) -> None:
        del authorization, request, now
        raise AuthorizationError(
            "security.live_observation_revocation_verifier_required"
        )


@dataclass(frozen=True, slots=True)
class LiveObservationExecutionAuthorizationVerifier:
    """Read current observation revocation state at every execution boundary."""

    provider: Callable[[], ObservationExecutionAuthorizationVerifier]

    def require_observation_valid(
        self,
        authorization: ObservationExecutionAuthorization,
        request: ObservationRequest,
        *,
        now: datetime,
    ) -> None:
        verifier = self.provider()
        if verifier is self or isinstance(
            verifier, DirectObservationExecutionAuthorizationVerifier
        ):
            raise AuthorizationError(
                "security.live_observation_revocation_verifier_invalid"
            )
        verifier.require_observation_valid(authorization, request, now=now)


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    policy_digest: str
    maximum_authorization_lifetime: timedelta = timedelta(hours=1)
    blocked_at_or_above: FindingSeverity = FindingSeverity.CRITICAL
    yolo_warning: str = (
        "YOLO MAXIMUM PRIVILEGE: safety restrictions are explicitly bypassed; "
        "identity, provenance, expiration, revocation, and audit remain enforced."
    )
    _known_privileges: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "network",
                "host-filesystem",
                "devices",
                "processes",
                "secrets",
                "compiler",
                "package-manager",
                "sandbox-escape",
            }
        )
    )
    _constrained_privileges: frozenset[str] = field(
        default_factory=lambda: frozenset({"compiler", "processes"})
    )
    _reviewed_privileges: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"compiler", "network", "package-manager", "processes"}
        )
    )
    _privileged_review_privileges: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "compiler",
                "devices",
                "host-filesystem",
                "network",
                "package-manager",
                "processes",
                "secrets",
            }
        )
    )

    def __post_init__(self) -> None:
        _require_digest(self.policy_digest, "policy_digest")
        if self.maximum_authorization_lifetime <= timedelta(0):
            raise ValueError("authorization lifetime must be positive")
        for privileges in (
            self._constrained_privileges,
            self._reviewed_privileges,
            self._privileged_review_privileges,
        ):
            if not privileges.issubset(self._known_privileges):
                raise ValueError("profile privileges must be known to the policy")

    def classify(
        self,
        *,
        effective_revision_digest: str,
        attestations: Iterable[OriginAttestation],
        findings: Iterable[SecurityFinding],
        dependency_classifications: Iterable[SecurityClassification] = (),
    ) -> SecurityClassification:
        attestation_values = tuple(attestations)
        finding_values = tuple(findings)
        dependency_values = tuple(dependency_classifications)
        if not attestation_values or any(
            not item.accepted for item in attestation_values
        ):
            raise AuthorizationError("security.origin_not_verified")
        source_digests = tuple(
            sorted({item.source_digest for item in attestation_values})
        )
        maximum = max(
            [
                *(item.severity for item in finding_values),
                *(item.maximum_severity for item in dependency_values),
            ],
            default=FindingSeverity.INFORMATIONAL,
        )
        dependency_blocked = any(
            item.profile is SecurityProfile.BLOCKED for item in dependency_values
        )
        profile = (
            SecurityProfile.BLOCKED
            if dependency_blocked or maximum >= self.blocked_at_or_above
            else SecurityProfile.CONSTRAINED
        )
        return SecurityClassification(
            effective_revision_digest=effective_revision_digest,
            source_digests=source_digests,
            dependency_classification_digests=tuple(
                sorted(item.digest for item in dependency_values)
            ),
            origin_attestation_digests=tuple(
                sorted(_canonical_digest(item.to_dict()) for item in attestation_values)
            ),
            finding_ids=tuple(sorted(item.finding_id for item in finding_values)),
            policy_digest=self.policy_digest,
            profile=profile,
            maximum_severity=maximum,
            permitted_privileges=(
                ()
                if profile is SecurityProfile.BLOCKED
                else tuple(sorted(self._constrained_privileges))
            ),
            decision_reason=(
                "dependency or finding reached the blocking threshold"
                if profile is SecurityProfile.BLOCKED
                else "verified source closure is allowed under constrained policy"
            ),
        )

    def authorize_build(
        self,
        classification: SecurityClassification,
        request: BuildRequest,
        *,
        actor: str,
        reason: str,
        issued_at: datetime,
        expires_at: datetime,
        yolo_acknowledged: bool = False,
    ) -> BuildAuthorization:
        if (
            classification.effective_revision_digest
            != request.effective_revision_digest
        ):
            raise AuthorizationError("security.classification_revision_mismatch")
        if classification.policy_digest != self.policy_digest:
            raise AuthorizationError("security.classification_policy_mismatch")
        if request.source_bundle_digest not in classification.source_digests:
            raise AuthorizationError("security.classification_source_mismatch")
        profile = classification.profile
        warning = None
        if profile in {SecurityProfile.BLOCKED, SecurityProfile.YOLO}:
            if not yolo_acknowledged:
                code = (
                    "security.build_blocked"
                    if profile is SecurityProfile.BLOCKED
                    else "security.build_yolo_not_acknowledged"
                )
                raise AuthorizationError(code)
        if yolo_acknowledged:
            profile = SecurityProfile.YOLO
            warning = self.yolo_warning
        self._validate_grant(
            actor=actor,
            reason=reason,
            issued_at=issued_at,
            expires_at=expires_at,
            privileges=request.requested_privileges,
            permitted_privileges=self._permitted_privileges(
                classification, profile=profile
            ),
        )
        request_digest = _canonical_digest(request.to_dict())
        issued = _utc(issued_at)
        expires = _utc(expires_at)
        privileges = tuple(sorted(request.requested_privileges))
        authorization_digest = _canonical_digest(
            {
                "classification_digest": classification.digest,
                "request_digest": request_digest,
                "actor": actor,
                "reason": reason,
                "profile": profile.value,
                "privileges": privileges,
                "issued_at": issued.isoformat(),
                "expires_at": expires.isoformat(),
                "warning": warning,
            }
        )
        return BuildAuthorization(
            authorization_id=(
                f"build-auth:{authorization_digest.removeprefix('sha256:')[:24]}"
            ),
            classification_digest=classification.digest,
            request_digest=request_digest,
            effective_revision_digest=request.effective_revision_digest,
            actor=actor,
            reason=reason,
            profile=profile,
            privileges=privileges,
            issued_at=issued,
            expires_at=expires,
            warning=warning,
        )

    def authorize_observation(
        self,
        classification: SecurityClassification,
        request: ObservationRequest,
        *,
        actor: str,
        reason: str,
        issued_at: datetime,
        expires_at: datetime,
        yolo_acknowledged: bool = False,
    ) -> ObservationExecutionAuthorization:
        if (
            classification.effective_revision_digest
            != request.effective_revision_digest
        ):
            raise AuthorizationError("security.classification_revision_mismatch")
        if classification.policy_digest != self.policy_digest:
            raise AuthorizationError("security.classification_policy_mismatch")
        if not set(request.source_digests).issubset(classification.source_digests):
            raise AuthorizationError("security.observation_source_mismatch")
        profile = classification.profile
        warning = None
        if profile in {SecurityProfile.BLOCKED, SecurityProfile.YOLO}:
            if not yolo_acknowledged:
                raise AuthorizationError("security.observation_yolo_not_acknowledged")
        if yolo_acknowledged:
            profile = SecurityProfile.YOLO
            warning = self.yolo_warning
        self._validate_grant(
            actor=actor,
            reason=reason,
            issued_at=issued_at,
            expires_at=expires_at,
            privileges=request.requested_privileges,
            permitted_privileges=self._permitted_privileges(
                classification,
                profile=profile,
            ),
        )
        request_digest = _canonical_digest(request.to_dict())
        issued = _utc(issued_at)
        expires = _utc(expires_at)
        privileges = tuple(sorted(request.requested_privileges))
        authorization_digest = _canonical_digest(
            {
                "classification_digest": classification.digest,
                "request_digest": request_digest,
                "actor": actor,
                "reason": reason,
                "profile": profile.value,
                "privileges": privileges,
                "issued_at": issued.isoformat(),
                "expires_at": expires.isoformat(),
                "warning": warning,
            }
        )
        return ObservationExecutionAuthorization(
            authorization_id=(
                f"observe-auth:{authorization_digest.removeprefix('sha256:')[:24]}"
            ),
            classification_digest=classification.digest,
            request_digest=request_digest,
            effective_revision_digest=request.effective_revision_digest,
            actor=actor,
            reason=reason,
            profile=profile,
            privileges=privileges,
            issued_at=issued,
            expires_at=expires,
            warning=warning,
        )

    def _validate_grant(
        self,
        *,
        actor: str,
        reason: str,
        issued_at: datetime,
        expires_at: datetime,
        privileges: tuple[str, ...],
        permitted_privileges: frozenset[str],
    ) -> None:
        issued = _utc(issued_at)
        expires = _utc(expires_at)
        if not actor.strip() or not reason.strip():
            raise AuthorizationError("security.grant_identity_required")
        if expires <= issued or expires - issued > self.maximum_authorization_lifetime:
            raise AuthorizationError("security.grant_expiration_invalid")
        requested = set(privileges)
        if not requested.issubset(self._known_privileges):
            raise AuthorizationError("security.privilege_unknown")
        if not requested.issubset(permitted_privileges):
            raise AuthorizationError("security.privilege_not_permitted")

    def _permitted_privileges(
        self,
        classification: SecurityClassification,
        *,
        profile: SecurityProfile | None = None,
    ) -> frozenset[str]:
        effective_profile = profile or classification.profile
        ceiling = {
            SecurityProfile.CONSTRAINED: self._constrained_privileges,
            SecurityProfile.REVIEWED: self._reviewed_privileges,
            SecurityProfile.PRIVILEGED_REVIEW: self._privileged_review_privileges,
            SecurityProfile.BLOCKED: frozenset(),
            SecurityProfile.YOLO: self._known_privileges,
        }[effective_profile]
        declared = frozenset(classification.permitted_privileges)
        if declared and effective_profile is not SecurityProfile.YOLO:
            return ceiling & declared
        return ceiling
