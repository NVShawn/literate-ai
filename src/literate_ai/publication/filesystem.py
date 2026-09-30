"""Explicit resumable filesystem publication and verified import."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Final

from literate_ai.contracts import ComponentRevisionRef, ContentIdentity, MigrationError
from literate_ai.security import SecurityProfile
from literate_ai.storage import (
    AppendOnlyEventStore,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRef,
    FileSystemCAS,
    StorageError,
    StorageSafetyError,
    canonical_json_bytes,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_URI = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUFFER_SIZE = 1024 * 1024
_FILESYSTEM_PUBLISHER_ID = "publisher:filesystem@1"
_FILESYSTEM_IMPORTER_ID = "importer:filesystem@1"
_PUBLICATION_MANIFEST_MEDIA_TYPE = (
    "application/vnd.literate-ai.publication-manifest+json"
)

PUBLICATION_REQUEST_SCHEMA: Final = "urn:literate-ai:schema:v2:publication-request"
PUBLICATION_AUTHORIZATION_SCHEMA: Final = (
    "urn:literate-ai:schema:v2:publication-authorization"
)
PUBLICATION_MANIFEST_SCHEMA: Final = "urn:literate-ai:schema:v2:publication-manifest"
IMPORT_REQUEST_SCHEMA: Final = "urn:literate-ai:schema:v2:import-request"
IMPORT_AUTHORIZATION_SCHEMA: Final = "urn:literate-ai:schema:v2:import-authorization"
TRANSFER_RECEIPT_SCHEMA: Final = "urn:literate-ai:schema:v2:transfer-receipt"

_PUBLICATION_REQUEST_FIELDS = frozenset(
    {
        "component_ref",
        "component_lock_identity",
        "effective_revision_digest",
        "source_bundle",
        "roots",
        "blobs",
        "provenance",
        "security_classification_digest",
        "security_profile",
        "publisher_id",
        "target_id",
        "target_identity_digest",
        "policy_digest",
        "actor",
    }
)
_AUTHORIZATION_FIELDS = frozenset(
    {
        "authorization_id",
        "request_digest",
        "policy_digest",
        "actor",
        "reason",
        "decision",
        "issued_at",
        "expires_at",
        "revoked",
    }
)
_PUBLICATION_MANIFEST_FIELDS = frozenset(
    {"schema_version", "request", "authorization", "created_at"}
)
_IMPORT_REQUEST_FIELDS = frozenset(
    {
        "publication_manifest",
        "publication_request_digest",
        "publication_authorization_id",
        "publication_policy_digest",
        "component_ref",
        "component_lock_identity",
        "effective_revision_digest",
        "source_bundle",
        "provenance",
        "security_classification_digest",
        "security_profile",
        "source_target_id",
        "source_target_identity_digest",
        "importer_id",
        "destination_identity_digest",
        "policy_digest",
        "actor",
    }
)
_TRANSFER_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "direction",
        "target_id",
        "target_identity_digest",
        "publication_manifest",
        "component_id",
        "revision",
        "component_ref",
        "component_lock_identity",
        "effective_revision_digest",
        "source_bundle",
        "provenance",
        "security_classification_digest",
        "security_profile",
        "publication_request_digest",
        "publication_authorization_id",
        "publication_policy_digest",
        "import_request_digest",
        "import_authorization_id",
        "import_policy_digest",
        "blob_count",
        "transferred_count",
        "reused_count",
        "started_at",
        "completed_at",
    }
)
_LEGACY_V3_PUBLICATION_REQUEST_FIELDS = _PUBLICATION_REQUEST_FIELDS - {
    "component_lock_identity"
}
_LEGACY_V3_IMPORT_REQUEST_FIELDS = _IMPORT_REQUEST_FIELDS - {"component_lock_identity"}
_LEGACY_V4_TRANSFER_RECEIPT_FIELDS = _TRANSFER_RECEIPT_FIELDS - {
    "component_lock_identity"
}
_LEGACY_MANIFEST_FIELDS = frozenset(
    {"schema_version", "component_id", "revision", "roots", "blobs", "created_at"}
)
_LEGACY_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "direction",
        "target_id",
        "publication_manifest",
        "component_id",
        "revision",
        "component_ref",
        "blob_count",
        "transferred_count",
        "reused_count",
        "started_at",
        "completed_at",
    }
)

_PUBLICATION_FIELDS: Final[dict[str, frozenset[str]]] = {
    PUBLICATION_REQUEST_SCHEMA: _PUBLICATION_REQUEST_FIELDS,
    PUBLICATION_AUTHORIZATION_SCHEMA: _AUTHORIZATION_FIELDS,
    PUBLICATION_MANIFEST_SCHEMA: _PUBLICATION_MANIFEST_FIELDS,
    IMPORT_REQUEST_SCHEMA: _IMPORT_REQUEST_FIELDS,
    IMPORT_AUTHORIZATION_SCHEMA: _AUTHORIZATION_FIELDS,
    TRANSFER_RECEIPT_SCHEMA: _TRANSFER_RECEIPT_FIELDS,
}
_LEGACY_V3_PUBLICATION_FIELDS: Final[dict[str, frozenset[str]]] = {
    PUBLICATION_REQUEST_SCHEMA: _LEGACY_V3_PUBLICATION_REQUEST_FIELDS,
    IMPORT_REQUEST_SCHEMA: _LEGACY_V3_IMPORT_REQUEST_FIELDS,
    TRANSFER_RECEIPT_SCHEMA: _LEGACY_V4_TRANSFER_RECEIPT_FIELDS,
}


def adapt_unreleased_post_v011_publication_document(
    value: Mapping[str, Any],
    *,
    expected_schema: str | None = None,
    legacy_component_lock_identity: ContentIdentity | None = None,
    legacy_manifest: PublicationManifest | None = None,
    legacy_manifest_document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a v2 envelope to one exact post-v0.1.1 publication field set.

    Request and authorization branches identify only the envelope.  Their matching
    typed ``from_dict`` readers remain the value-validation boundary.  Manifest and
    receipt branches are canonicalized through those typed readers here.
    """

    document = _strict_object(value, "legacy publication document")
    if "schema" in document:
        raise ValueError("publication legacy adapter does not accept versioned input")
    current_matches = [
        schema
        for schema, fields in _PUBLICATION_FIELDS.items()
        if set(document) == fields
    ]
    legacy_matches = [
        schema
        for schema, fields in _LEGACY_V3_PUBLICATION_FIELDS.items()
        if set(document) == fields
    ]
    matches = current_matches + legacy_matches
    if expected_schema is not None:
        if expected_schema not in matches:
            raise ValueError(
                "publication fields do not match the expected legacy shape"
            )
        matches = [expected_schema]
    if len(matches) != 1:
        raise ValueError(
            "publication fields do not identify one supported legacy shape"
        )
    schema = matches[0]
    if schema == PUBLICATION_MANIFEST_SCHEMA:
        if document.get("schema_version") != 3:
            raise ValueError(
                "only post-v0.1.1 publication schema version 3 is adaptable"
            )
        return PublicationManifest.from_dict(
            document,
            legacy_component_lock_identity=legacy_component_lock_identity,
        ).to_dict()
    if schema == TRANSFER_RECEIPT_SCHEMA:
        if document.get("schema_version") != 3:
            raise ValueError(
                "only post-v0.1.1 publication schema version 3 is adaptable"
            )
        return TransferReceipt.from_dict(
            document,
            legacy_manifest=legacy_manifest,
            legacy_manifest_document=legacy_manifest_document,
        ).to_dict()
    if schema == PUBLICATION_REQUEST_SCHEMA and schema in legacy_matches:
        return PublicationRequest.from_dict(
            {"schema": schema, **document},
            legacy_component_lock_identity=legacy_component_lock_identity,
        ).to_dict()
    if schema == IMPORT_REQUEST_SCHEMA and schema in legacy_matches:
        return ImportRequest.from_dict(
            {"schema": schema, **document},
            legacy_component_lock_identity=legacy_component_lock_identity,
        ).to_dict()
    return {"schema": schema, **document}


def normalize_publication_document(
    value: Mapping[str, Any],
    *,
    expected_schema: str | None = None,
    legacy_component_lock_identity: ContentIdentity | None = None,
    legacy_manifest: PublicationManifest | None = None,
    legacy_manifest_document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a publication envelope; matching typed readers validate values."""

    document = _strict_object(value, "publication document")
    if "schema" not in document:
        return adapt_unreleased_post_v011_publication_document(
            document,
            expected_schema=expected_schema,
            legacy_component_lock_identity=legacy_component_lock_identity,
            legacy_manifest=legacy_manifest,
            legacy_manifest_document=legacy_manifest_document,
        )
    schema = document["schema"]
    if not isinstance(schema, str) or schema not in _PUBLICATION_FIELDS:
        raise MigrationError(
            "contracts.migration_schema_unsupported",
            f"publication schema is unsupported: {schema!r}",
        )
    if expected_schema is not None and schema != expected_schema:
        raise ValueError(f"expected {expected_schema}, got {schema!r}")
    if schema in {PUBLICATION_MANIFEST_SCHEMA, TRANSFER_RECEIPT_SCHEMA}:
        version = _require_integer(
            document.get("schema_version"),
            "publication schema_version",
            minimum=None,
        )
        if schema == PUBLICATION_MANIFEST_SCHEMA and version == 4:
            return PublicationManifest.from_dict(
                document,
                legacy_component_lock_identity=legacy_component_lock_identity,
            ).to_dict()
        if schema == TRANSFER_RECEIPT_SCHEMA and version == 4:
            return TransferReceipt.from_dict(
                document,
                legacy_manifest=legacy_manifest,
                legacy_manifest_document=legacy_manifest_document,
            ).to_dict()
    if set(document) != _PUBLICATION_FIELDS[schema] | {"schema"}:
        raise ValueError("publication fields do not match the current schema")
    if schema in {PUBLICATION_MANIFEST_SCHEMA, TRANSFER_RECEIPT_SCHEMA}:
        version = _require_integer(
            document["schema_version"], "publication schema_version", minimum=None
        )
        if version != 5:
            code = (
                "contracts.migration_future_schema"
                if version > 5
                else "contracts.migration_schema_unsupported"
            )
            raise MigrationError(code, f"unsupported publication schema {version}")
    return document


def _publication_document(
    value: Mapping[str, Any], expected_schema: str
) -> dict[str, Any]:
    return normalize_publication_document(value, expected_schema=expected_schema)


class PublicationError(RuntimeError):
    """A publication or import operation failed safely."""


_STABLE_TRANSFER_FAILURE_TYPES: tuple[tuple[type[Exception], str], ...] = (
    (PermissionError, "PermissionError"),
    (TimeoutError, "TimeoutError"),
    (OSError, "OSError"),
    (TypeError, "TypeError"),
    (ValueError, "ValueError"),
    (RuntimeError, "RuntimeError"),
)


def _stable_transfer_failure_type(error: Exception) -> str:
    """Classify transfer failures without persisting exception-controlled text."""

    return next(
        (
            category
            for exception_type, category in _STABLE_TRANSFER_FAILURE_TYPES
            if isinstance(error, exception_type)
        ),
        "Exception",
    )


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    """Exact publication payload and policy inputs, before authorization."""

    SCHEMA: ClassVar[str] = PUBLICATION_REQUEST_SCHEMA

    component_ref: ComponentRevisionRef
    component_lock_identity: ContentIdentity
    effective_revision_digest: str
    source_bundle: BlobRef
    roots: dict[str, BlobRef]
    blobs: tuple[BlobRef, ...]
    provenance: tuple[BlobRef, ...]
    security_classification_digest: str
    security_profile: SecurityProfile
    publisher_id: str
    target_id: str
    target_identity_digest: str
    policy_digest: str
    actor: str

    def __post_init__(self) -> None:
        if not isinstance(self.component_ref, ComponentRevisionRef):
            raise ValueError("publication requires an exact Component revision ref")
        if not isinstance(self.component_lock_identity, ContentIdentity):
            raise ValueError("publication requires an exact Component lock identity")
        if not isinstance(self.roots, dict):
            raise ValueError("publication roots must be a dictionary")
        if not isinstance(self.blobs, tuple) or not isinstance(self.provenance, tuple):
            raise ValueError("publication blobs and provenance must be tuples")
        _require_digest(self.effective_revision_digest, "effective revision")
        _require_digest(self.security_classification_digest, "security classification")
        _require_digest(self.policy_digest, "publication policy")
        _require_digest(self.target_identity_digest, "publication target")
        _validate_id(self.component_ref.coordinate.name, "component")
        _validate_id(self.target_id, "target")
        _require_text(self.publisher_id, "publication publisher_id")
        _require_text(self.actor, "publication actor")
        if not isinstance(self.security_profile, SecurityProfile):
            raise ValueError("publication security profile is invalid")
        _validate_blob_ref(self.source_bundle, "publication.source_bundle")
        _validate_blob_closure(self.roots, self.blobs)
        declared = {item.identity for item in self.blobs}
        if self.source_bundle.identity not in declared:
            raise ValueError("publication source bundle is not in the blob set")
        if self.source_bundle.identity not in {
            item.identity for item in self.roots.values()
        }:
            raise ValueError("publication source bundle must be a declared root")
        if not self.provenance:
            raise ValueError("publication requires exact provenance")
        for index, item in enumerate(self.provenance):
            _validate_blob_ref(item, f"publication.provenance[{index}]")
        provenance = [item.identity for item in self.provenance]
        if len(provenance) != len(set(provenance)):
            raise ValueError("publication contains duplicate provenance")
        if not set(provenance).issubset(declared):
            raise ValueError("publication provenance is not in the blob set")

    @property
    def component_id(self) -> str:
        return self.component_ref.coordinate.name

    @property
    def revision(self) -> str:
        return self.component_ref.revision_identity.uri

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())

    @classmethod
    def create(
        cls,
        *,
        component_ref: ComponentRevisionRef,
        component_lock_identity: ContentIdentity,
        effective_revision_digest: str,
        source_bundle: BlobRef,
        roots: Mapping[str, BlobRef],
        blobs: Iterable[BlobRef],
        provenance: Iterable[BlobRef],
        security_classification_digest: str,
        security_profile: SecurityProfile,
        target_id: str,
        target_identity_digest: str,
        policy_digest: str,
        actor: str,
        publisher_id: str = _FILESYSTEM_PUBLISHER_ID,
    ) -> PublicationRequest:
        provenance_values = tuple(provenance)
        unique = {item.identity: item for item in blobs}
        unique.update((item.identity, item) for item in roots.values())
        unique[source_bundle.identity] = source_bundle
        unique.update((item.identity, item) for item in provenance_values)
        return cls(
            component_ref=component_ref,
            component_lock_identity=component_lock_identity,
            effective_revision_digest=effective_revision_digest,
            source_bundle=source_bundle,
            roots=dict(sorted(roots.items())),
            blobs=tuple(unique[key] for key in sorted(unique)),
            provenance=tuple(sorted(provenance_values, key=lambda item: item.identity)),
            security_classification_digest=security_classification_digest,
            security_profile=security_profile,
            publisher_id=publisher_id,
            target_id=target_id,
            target_identity_digest=target_identity_digest,
            policy_digest=policy_digest,
            actor=actor,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "component_ref": self.component_ref.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "effective_revision_digest": self.effective_revision_digest,
            "source_bundle": self.source_bundle.to_dict(),
            "roots": {
                key: value.to_dict() for key, value in sorted(self.roots.items())
            },
            "blobs": [item.to_dict() for item in self.blobs],
            "provenance": [item.to_dict() for item in self.provenance],
            "security_classification_digest": self.security_classification_digest,
            "security_profile": self.security_profile.value,
            "publisher_id": self.publisher_id,
            "target_id": self.target_id,
            "target_identity_digest": self.target_identity_digest,
            "policy_digest": self.policy_digest,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        legacy_component_lock_identity: ContentIdentity | None = None,
    ) -> PublicationRequest:
        envelope = _strict_object(value, "publication request")
        if "component_lock_identity" not in envelope:
            if not isinstance(legacy_component_lock_identity, ContentIdentity):
                raise MigrationError(
                    "contracts.migration_legacy_ambiguous",
                    "legacy publication request lacks an exact Component lock identity",
                )
            required = _LEGACY_V3_PUBLICATION_REQUEST_FIELDS
            if "schema" in envelope:
                if envelope["schema"] != cls.SCHEMA:
                    raise MigrationError(
                        "contracts.migration_schema_unsupported",
                        "legacy publication request schema is unsupported",
                    )
                required = required | {"schema"}
            _strict_fields(envelope, required, "legacy publication request")
            envelope = {
                **envelope,
                "schema": cls.SCHEMA,
                "component_lock_identity": (legacy_component_lock_identity.to_dict()),
            }
        data = _strict_fields(
            _publication_document(envelope, cls.SCHEMA),
            _PUBLICATION_REQUEST_FIELDS | {"schema"},
            "publication request",
        )
        request = cls(
            component_ref=ComponentRevisionRef.from_dict(data["component_ref"]),
            component_lock_identity=ContentIdentity.from_dict(
                data["component_lock_identity"],
                path="publication request.component_lock_identity",
            ),
            effective_revision_digest=_require_text(
                data["effective_revision_digest"],
                "publication request.effective_revision_digest",
            ),
            source_bundle=_parse_blob_ref(
                data["source_bundle"], "publication request.source_bundle"
            ),
            roots=_parse_blob_roots(data["roots"], "publication request.roots"),
            blobs=_parse_blob_array(data["blobs"], "publication request.blobs"),
            provenance=_parse_blob_array(
                data["provenance"], "publication request.provenance"
            ),
            security_classification_digest=_require_text(
                data["security_classification_digest"],
                "publication request.security_classification_digest",
            ),
            security_profile=SecurityProfile(
                _require_text(
                    data["security_profile"],
                    "publication request.security_profile",
                )
            ),
            publisher_id=_require_text(
                data["publisher_id"], "publication request.publisher_id"
            ),
            target_id=_require_text(data["target_id"], "publication request.target_id"),
            target_identity_digest=_require_text(
                data["target_identity_digest"],
                "publication request.target_identity_digest",
            ),
            policy_digest=_require_text(
                data["policy_digest"], "publication request.policy_digest"
            ),
            actor=_require_text(data["actor"], "publication request.actor"),
        )
        if (
            legacy_component_lock_identity is not None
            and request.component_lock_identity != legacy_component_lock_identity
        ):
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "publication request Component lock differs from migration context",
            )
        return request


@dataclass(frozen=True, slots=True)
class PublicationAuthorization:
    """Short-lived policy decision over one exact publication request."""

    SCHEMA: ClassVar[str] = PUBLICATION_AUTHORIZATION_SCHEMA

    authorization_id: str
    request_digest: str
    policy_digest: str
    actor: str
    reason: str
    decision: str
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_text(self.authorization_id, "publication authorization_id")
        _require_digest(self.request_digest, "publication request")
        _require_digest(self.policy_digest, "publication policy")
        _require_text(self.actor, "publication authorization actor")
        _require_text(self.reason, "publication authorization reason")
        _require_text(self.decision, "publication authorization decision")
        _utc(self.issued_at)
        _utc(self.expires_at)
        _require_boolean(self.revoked, "publication authorization revoked")
        if self.decision != "allow":
            raise ValueError("only explicit allow decisions authorize publication")
        if _utc(self.expires_at) <= _utc(self.issued_at):
            raise ValueError("publication authorization expiration is invalid")
        if self.authorization_id != self._expected_id():
            raise ValueError("publication authorization identity is invalid")

    @classmethod
    def issue(
        cls,
        request: PublicationRequest,
        *,
        reason: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> PublicationAuthorization:
        values = {
            "request_digest": request.digest,
            "policy_digest": request.policy_digest,
            "actor": request.actor,
            "reason": reason,
            "decision": "allow",
            "issued_at": _utc(issued_at).isoformat(),
            "expires_at": _utc(expires_at).isoformat(),
        }
        identity = _canonical_digest(values).removeprefix("sha256:")[:24]
        return cls(
            authorization_id=f"publication-auth:{identity}",
            request_digest=request.digest,
            policy_digest=request.policy_digest,
            actor=request.actor,
            reason=reason,
            decision="allow",
            issued_at=_utc(issued_at),
            expires_at=_utc(expires_at),
        )

    def require_valid(self, request: PublicationRequest, *, now: datetime) -> None:
        if self.revoked:
            raise PublicationError("publication authorization is revoked")
        current = _utc(now)
        if current < _utc(self.issued_at):
            raise PublicationError("publication authorization is not yet valid")
        if current >= _utc(self.expires_at):
            raise PublicationError("publication authorization is expired")
        if (
            self.request_digest != request.digest
            or self.policy_digest != request.policy_digest
            or self.actor != request.actor
            or self.decision != "allow"
        ):
            raise PublicationError("publication authorization does not match request")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "authorization_id": self.authorization_id,
            "request_digest": self.request_digest,
            "policy_digest": self.policy_digest,
            "actor": self.actor,
            "reason": self.reason,
            "decision": self.decision,
            "issued_at": _utc(self.issued_at).isoformat(),
            "expires_at": _utc(self.expires_at).isoformat(),
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PublicationAuthorization:
        data = _strict_fields(
            _publication_document(value, cls.SCHEMA),
            _AUTHORIZATION_FIELDS | {"schema"},
            "publication authorization",
        )
        return cls(
            authorization_id=_require_text(
                data["authorization_id"],
                "publication authorization.authorization_id",
            ),
            request_digest=_require_text(
                data["request_digest"], "publication authorization.request_digest"
            ),
            policy_digest=_require_text(
                data["policy_digest"], "publication authorization.policy_digest"
            ),
            actor=_require_text(data["actor"], "publication authorization.actor"),
            reason=_require_text(data["reason"], "publication authorization.reason"),
            decision=_require_text(
                data["decision"], "publication authorization.decision"
            ),
            issued_at=_parse_datetime(
                data["issued_at"], "publication authorization.issued_at"
            ),
            expires_at=_parse_datetime(
                data["expires_at"], "publication authorization.expires_at"
            ),
            revoked=_require_boolean(
                data["revoked"], "publication authorization.revoked"
            ),
        )

    def _expected_id(self) -> str:
        values = self.to_dict()
        values.pop("schema")
        values.pop("authorization_id")
        values.pop("revoked")
        identity = _canonical_digest(values).removeprefix("sha256:")[:24]
        return f"publication-auth:{identity}"


@dataclass(frozen=True, slots=True)
class PublicationPolicy:
    """Explicit target/profile allowlist and revocation-aware publication policy."""

    policy_digest: str
    allowed_targets: tuple[str, ...]
    allowed_publishers: tuple[str, ...] = (_FILESYSTEM_PUBLISHER_ID,)
    permitted_profiles: tuple[SecurityProfile, ...] = (
        SecurityProfile.CONSTRAINED,
        SecurityProfile.REVIEWED,
        SecurityProfile.PRIVILEGED_REVIEW,
    )
    maximum_authorization_lifetime: timedelta = timedelta(minutes=10)
    revoked_authorization_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_digest(self.policy_digest, "publication policy")
        if not self.allowed_targets or not self.allowed_publishers:
            raise ValueError(
                "publication policy requires target and publisher allowlists"
            )
        for target in self.allowed_targets:
            _validate_id(target, "target")
        if any(not item.strip() for item in self.allowed_publishers):
            raise ValueError("publication publisher identities cannot be empty")
        if not self.permitted_profiles or any(
            not isinstance(item, SecurityProfile) for item in self.permitted_profiles
        ):
            raise ValueError("publication policy requires security profiles")
        if self.maximum_authorization_lifetime <= timedelta(0):
            raise ValueError("publication authorization lifetime must be positive")
        if any(not item for item in self.revoked_authorization_ids) or len(
            self.revoked_authorization_ids
        ) != len(set(self.revoked_authorization_ids)):
            raise ValueError("publication revocation identities must be unique")

    def authorize(
        self,
        request: PublicationRequest,
        *,
        reason: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> PublicationAuthorization:
        self._require_request_allowed(request)
        issued = _utc(issued_at)
        expires = _utc(expires_at)
        if (
            not reason.strip()
            or expires <= issued
            or expires - issued > self.maximum_authorization_lifetime
        ):
            raise PublicationError("publication authorization parameters are invalid")
        return PublicationAuthorization.issue(
            request,
            reason=reason,
            issued_at=issued,
            expires_at=expires,
        )

    def require_publish_valid(
        self,
        request: PublicationRequest,
        authorization: PublicationAuthorization,
        *,
        target_id: str,
        target_identity_digest: str,
        publisher_id: str,
        now: datetime,
    ) -> None:
        if (
            target_id != request.target_id
            or target_identity_digest != request.target_identity_digest
            or publisher_id != request.publisher_id
        ):
            raise PublicationError("publication request targets another publisher")
        self._require_request_allowed(request)
        if authorization.authorization_id in self.revoked_authorization_ids:
            raise PublicationError("publication authorization is revoked by policy")
        if (
            _utc(authorization.expires_at) - _utc(authorization.issued_at)
            > self.maximum_authorization_lifetime
        ):
            raise PublicationError(
                "publication authorization exceeds the policy lifetime"
            )
        authorization.require_valid(request, now=now)

    def _require_request_allowed(self, request: PublicationRequest) -> None:
        if request.policy_digest != self.policy_digest:
            raise PublicationError("publication request targets another policy")
        if request.target_id not in self.allowed_targets:
            raise PublicationError("publication target is not permitted by policy")
        if request.publisher_id not in self.allowed_publishers:
            raise PublicationError("publisher is not permitted by policy")
        if request.security_profile not in self.permitted_profiles:
            raise PublicationError("security profile is not publishable by policy")


@dataclass(frozen=True, slots=True)
class PublicationManifest:
    """Immutable authorized request for one exact Component revision."""

    SCHEMA: ClassVar[str] = PUBLICATION_MANIFEST_SCHEMA

    request: PublicationRequest
    authorization: PublicationAuthorization
    created_at: str
    schema_version: int = 5

    def __post_init__(self) -> None:
        if (
            _require_integer(
                self.schema_version,
                "publication manifest.schema_version",
                minimum=1,
            )
            != 5
        ):
            raise ValueError("unsupported publication manifest schema")
        if not isinstance(self.request, PublicationRequest):
            raise ValueError("publication manifest request is invalid")
        if not isinstance(self.authorization, PublicationAuthorization):
            raise ValueError("publication manifest authorization is invalid")
        created = _parse_datetime(self.created_at, "publication manifest.created_at")
        self.authorization.require_valid(self.request, now=created)

    @property
    def component_id(self) -> str:
        return self.request.component_id

    @property
    def revision(self) -> str:
        return self.request.revision

    @property
    def component_ref(self) -> ComponentRevisionRef:
        return self.request.component_ref

    @property
    def component_lock_identity(self) -> ContentIdentity:
        return self.request.component_lock_identity

    @property
    def roots(self) -> dict[str, BlobRef]:
        return self.request.roots

    @property
    def blobs(self) -> tuple[BlobRef, ...]:
        return self.request.blobs

    @classmethod
    def create(
        cls,
        *,
        request: PublicationRequest,
        authorization: PublicationAuthorization,
        created_at: datetime | None = None,
    ) -> PublicationManifest:
        return cls(
            request=request,
            authorization=authorization,
            created_at=(created_at or datetime.now(UTC)).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.schema_version,
            "request": self.request.to_dict(),
            "authorization": self.authorization.to_dict(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        legacy_component_ref: ComponentRevisionRef | None = None,
        legacy_request: PublicationRequest | None = None,
        legacy_authorization: PublicationAuthorization | None = None,
        legacy_component_lock_identity: ContentIdentity | None = None,
    ) -> PublicationManifest:
        envelope = _strict_object(value, "publication manifest")
        if "schema_version" not in envelope:
            raise ValueError("publication manifest is missing field: schema_version")
        schema_version = _require_integer(
            envelope["schema_version"],
            "publication manifest.schema_version",
            minimum=None,
        )
        if schema_version > 5:
            raise MigrationError(
                "contracts.migration_future_schema",
                f"cannot read future publication schema {schema_version}",
            )
        if schema_version < 1:
            raise MigrationError(
                "contracts.migration_schema_unsupported",
                f"unsupported publication schema {schema_version}",
            )
        if "schema" in envelope and schema_version == 5:
            data = _strict_fields(
                normalize_publication_document(envelope, expected_schema=cls.SCHEMA),
                _PUBLICATION_MANIFEST_FIELDS | {"schema"},
                "publication manifest",
            )
            request_value = _strict_object(
                data["request"], "publication manifest.request"
            )
            authorization_value = _strict_object(
                data["authorization"], "publication manifest.authorization"
            )
            if request_value.get("schema") != PUBLICATION_REQUEST_SCHEMA:
                raise ValueError("publication manifest requires a v2 request")
            if authorization_value.get("schema") != PUBLICATION_AUTHORIZATION_SCHEMA:
                raise ValueError("publication manifest requires a v2 authorization")
            return cls(
                schema_version=5,
                request=PublicationRequest.from_dict(request_value),
                authorization=PublicationAuthorization.from_dict(authorization_value),
                created_at=_require_text(
                    data["created_at"], "publication manifest.created_at"
                ),
            )
        if schema_version == 5:
            raise MigrationError(
                "contracts.migration_schema_unsupported",
                "publication schema v5 requires its v2 schema identity",
            )
        if schema_version in {3, 4}:
            if schema_version == 4 and envelope.get("schema") != cls.SCHEMA:
                raise MigrationError(
                    "contracts.migration_schema_unsupported",
                    "publication schema v4 requires its v2 schema identity",
                )
            data = _strict_fields(
                envelope,
                (
                    _PUBLICATION_MANIFEST_FIELDS | {"schema"}
                    if schema_version == 4
                    else _PUBLICATION_MANIFEST_FIELDS
                ),
                (
                    "publication v4 manifest"
                    if schema_version == 4
                    else "post-v0.1.1 publication manifest"
                ),
            )
            request_value = _strict_object(
                data["request"], "legacy publication manifest.request"
            )
            authorization_value = _strict_object(
                data["authorization"], "legacy publication manifest.authorization"
            )
            legacy_authorization_value = PublicationAuthorization.from_dict(
                authorization_value
            )
            legacy_request_digest = _canonical_digest(request_value)
            request = PublicationRequest.from_dict(
                request_value,
                legacy_component_lock_identity=legacy_component_lock_identity,
            )
            if (
                legacy_authorization_value.request_digest != legacy_request_digest
                or legacy_authorization_value.policy_digest != request.policy_digest
                or legacy_authorization_value.actor != request.actor
            ):
                raise MigrationError(
                    "contracts.migration_legacy_ambiguous",
                    "publication v3 authorization does not bind its request",
                )
            authorization = PublicationAuthorization.issue(
                request,
                reason=legacy_authorization_value.reason,
                issued_at=legacy_authorization_value.issued_at,
                expires_at=legacy_authorization_value.expires_at,
            )
            return cls(
                schema_version=5,
                request=request,
                authorization=authorization,
                created_at=_require_text(
                    data["created_at"], "publication manifest.created_at"
                ),
            )
        if legacy_request is None or legacy_authorization is None:
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "publication v1/v2 lacks source, provenance, and policy decision",
            )
        if schema_version == 1 and legacy_component_ref is None:
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "publication v1 lacks Component coordinate and semantic version",
            )
        required = (
            _LEGACY_MANIFEST_FIELDS | {"component_ref"}
            if schema_version == 2
            else _LEGACY_MANIFEST_FIELDS
        )
        data = _strict_fields(envelope, required, "legacy publication manifest")
        component_ref = (
            ComponentRevisionRef.from_dict(data["component_ref"])
            if schema_version == 2
            else legacy_component_ref
        )
        parsed_roots = _parse_blob_roots(
            data["roots"], "legacy publication manifest.roots"
        )
        parsed_blobs = _parse_blob_array(
            data["blobs"], "legacy publication manifest.blobs"
        )
        if (
            component_ref != legacy_request.component_ref
            or _require_text(
                data["component_id"], "legacy publication manifest.component_id"
            )
            != legacy_request.component_id
            or _require_text(data["revision"], "legacy publication manifest.revision")
            != legacy_request.revision
            or parsed_roots != legacy_request.roots
            or {item.identity for item in parsed_blobs}
            != {item.identity for item in legacy_request.blobs}
        ):
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "legacy publication facts do not match supplied migration context",
            )
        return cls(
            schema_version=5,
            request=legacy_request,
            authorization=legacy_authorization,
            created_at=_require_text(
                data["created_at"], "legacy publication manifest.created_at"
            ),
        )


@dataclass(frozen=True, slots=True)
class ImportRequest:
    """Exact remote release and local destination proposed for import."""

    SCHEMA: ClassVar[str] = IMPORT_REQUEST_SCHEMA

    publication_manifest: BlobRef
    publication_request_digest: str
    publication_authorization_id: str
    publication_policy_digest: str
    component_ref: ComponentRevisionRef
    component_lock_identity: ContentIdentity
    effective_revision_digest: str
    source_bundle: BlobRef
    provenance: tuple[BlobRef, ...]
    security_classification_digest: str
    security_profile: SecurityProfile
    source_target_id: str
    source_target_identity_digest: str
    importer_id: str
    destination_identity_digest: str
    policy_digest: str
    actor: str

    def __post_init__(self) -> None:
        if not isinstance(self.component_ref, ComponentRevisionRef):
            raise ValueError("import requires an exact Component revision ref")
        if not isinstance(self.component_lock_identity, ContentIdentity):
            raise ValueError("import requires an exact Component lock identity")
        if not isinstance(self.provenance, tuple):
            raise ValueError("import provenance must be a tuple")
        for value, label in (
            (self.publication_request_digest, "publication request"),
            (self.publication_policy_digest, "publication policy"),
            (self.effective_revision_digest, "effective revision"),
            (self.security_classification_digest, "security classification"),
            (self.source_target_identity_digest, "source target"),
            (self.destination_identity_digest, "import destination"),
            (self.policy_digest, "import policy"),
        ):
            _require_digest(value, label)
        _validate_id(self.component_ref.coordinate.name, "component")
        _validate_id(self.source_target_id, "target")
        _require_text(
            self.publication_authorization_id,
            "import publication_authorization_id",
        )
        _require_text(self.importer_id, "import importer_id")
        _require_text(self.actor, "import actor")
        if not isinstance(self.security_profile, SecurityProfile):
            raise ValueError("import security profile is invalid")
        _validate_blob_ref(self.publication_manifest, "import.publication_manifest")
        _validate_blob_ref(self.source_bundle, "import.source_bundle")
        if not self.provenance:
            raise ValueError("import requires exact provenance")
        for index, item in enumerate(self.provenance):
            _validate_blob_ref(item, f"import.provenance[{index}]")
        identities = tuple(item.identity for item in self.provenance)
        if len(identities) != len(set(identities)):
            raise ValueError("import provenance must be unique")

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())

    @classmethod
    def create(
        cls,
        *,
        manifest: PublicationManifest,
        publication_manifest: BlobRef,
        destination_identity_digest: str,
        policy_digest: str,
        actor: str,
        importer_id: str = _FILESYSTEM_IMPORTER_ID,
    ) -> ImportRequest:
        request = manifest.request
        return cls(
            publication_manifest=publication_manifest,
            publication_request_digest=request.digest,
            publication_authorization_id=manifest.authorization.authorization_id,
            publication_policy_digest=request.policy_digest,
            component_ref=request.component_ref,
            component_lock_identity=request.component_lock_identity,
            effective_revision_digest=request.effective_revision_digest,
            source_bundle=request.source_bundle,
            provenance=request.provenance,
            security_classification_digest=request.security_classification_digest,
            security_profile=request.security_profile,
            source_target_id=request.target_id,
            source_target_identity_digest=request.target_identity_digest,
            importer_id=importer_id,
            destination_identity_digest=destination_identity_digest,
            policy_digest=policy_digest,
            actor=actor,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "publication_manifest": self.publication_manifest.to_dict(),
            "publication_request_digest": self.publication_request_digest,
            "publication_authorization_id": self.publication_authorization_id,
            "publication_policy_digest": self.publication_policy_digest,
            "component_ref": self.component_ref.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "effective_revision_digest": self.effective_revision_digest,
            "source_bundle": self.source_bundle.to_dict(),
            "provenance": [item.to_dict() for item in self.provenance],
            "security_classification_digest": self.security_classification_digest,
            "security_profile": self.security_profile.value,
            "source_target_id": self.source_target_id,
            "source_target_identity_digest": self.source_target_identity_digest,
            "importer_id": self.importer_id,
            "destination_identity_digest": self.destination_identity_digest,
            "policy_digest": self.policy_digest,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        legacy_component_lock_identity: ContentIdentity | None = None,
    ) -> ImportRequest:
        envelope = _strict_object(value, "import request")
        if "component_lock_identity" not in envelope:
            if not isinstance(legacy_component_lock_identity, ContentIdentity):
                raise MigrationError(
                    "contracts.migration_legacy_ambiguous",
                    "legacy import request lacks an exact Component lock identity",
                )
            required = _LEGACY_V3_IMPORT_REQUEST_FIELDS
            if "schema" in envelope:
                if envelope["schema"] != cls.SCHEMA:
                    raise MigrationError(
                        "contracts.migration_schema_unsupported",
                        "legacy import request schema is unsupported",
                    )
                required = required | {"schema"}
            _strict_fields(envelope, required, "legacy import request")
            envelope = {
                **envelope,
                "schema": cls.SCHEMA,
                "component_lock_identity": (legacy_component_lock_identity.to_dict()),
            }
        data = _strict_fields(
            _publication_document(envelope, cls.SCHEMA),
            _IMPORT_REQUEST_FIELDS | {"schema"},
            "import request",
        )
        request = cls(
            publication_manifest=_parse_blob_ref(
                data["publication_manifest"], "import request.publication_manifest"
            ),
            publication_request_digest=_require_text(
                data["publication_request_digest"],
                "import request.publication_request_digest",
            ),
            publication_authorization_id=_require_text(
                data["publication_authorization_id"],
                "import request.publication_authorization_id",
            ),
            publication_policy_digest=_require_text(
                data["publication_policy_digest"],
                "import request.publication_policy_digest",
            ),
            component_ref=ComponentRevisionRef.from_dict(data["component_ref"]),
            component_lock_identity=ContentIdentity.from_dict(
                data["component_lock_identity"],
                path="import request.component_lock_identity",
            ),
            effective_revision_digest=_require_text(
                data["effective_revision_digest"],
                "import request.effective_revision_digest",
            ),
            source_bundle=_parse_blob_ref(
                data["source_bundle"], "import request.source_bundle"
            ),
            provenance=_parse_blob_array(
                data["provenance"], "import request.provenance"
            ),
            security_classification_digest=_require_text(
                data["security_classification_digest"],
                "import request.security_classification_digest",
            ),
            security_profile=SecurityProfile(
                _require_text(
                    data["security_profile"], "import request.security_profile"
                )
            ),
            source_target_id=_require_text(
                data["source_target_id"], "import request.source_target_id"
            ),
            source_target_identity_digest=_require_text(
                data["source_target_identity_digest"],
                "import request.source_target_identity_digest",
            ),
            importer_id=_require_text(
                data["importer_id"], "import request.importer_id"
            ),
            destination_identity_digest=_require_text(
                data["destination_identity_digest"],
                "import request.destination_identity_digest",
            ),
            policy_digest=_require_text(
                data["policy_digest"], "import request.policy_digest"
            ),
            actor=_require_text(data["actor"], "import request.actor"),
        )
        if (
            legacy_component_lock_identity is not None
            and request.component_lock_identity != legacy_component_lock_identity
        ):
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "import request Component lock differs from migration context",
            )
        return request


@dataclass(frozen=True, slots=True)
class ImportAuthorization:
    """Short-lived local policy decision over one exact import request."""

    SCHEMA: ClassVar[str] = IMPORT_AUTHORIZATION_SCHEMA

    authorization_id: str
    request_digest: str
    policy_digest: str
    actor: str
    reason: str
    decision: str
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_text(self.authorization_id, "import authorization_id")
        _require_digest(self.request_digest, "import request")
        _require_digest(self.policy_digest, "import policy")
        _require_text(self.actor, "import authorization actor")
        _require_text(self.reason, "import authorization reason")
        _require_text(self.decision, "import authorization decision")
        _utc(self.issued_at)
        _utc(self.expires_at)
        _require_boolean(self.revoked, "import authorization revoked")
        if self.decision != "allow":
            raise ValueError("only explicit allow decisions authorize import")
        if _utc(self.expires_at) <= _utc(self.issued_at):
            raise ValueError("import authorization expiration is invalid")
        if self.authorization_id != self._expected_id():
            raise ValueError("import authorization identity is invalid")

    @classmethod
    def issue(
        cls,
        request: ImportRequest,
        *,
        reason: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> ImportAuthorization:
        values = {
            "request_digest": request.digest,
            "policy_digest": request.policy_digest,
            "actor": request.actor,
            "reason": reason,
            "decision": "allow",
            "issued_at": _utc(issued_at).isoformat(),
            "expires_at": _utc(expires_at).isoformat(),
        }
        identity = _canonical_digest(values).removeprefix("sha256:")[:24]
        return cls(
            authorization_id=f"import-auth:{identity}",
            request_digest=request.digest,
            policy_digest=request.policy_digest,
            actor=request.actor,
            reason=reason,
            decision="allow",
            issued_at=_utc(issued_at),
            expires_at=_utc(expires_at),
        )

    def require_valid(self, request: ImportRequest, *, now: datetime) -> None:
        if self.revoked:
            raise PublicationError("import authorization is revoked")
        current = _utc(now)
        if current < _utc(self.issued_at):
            raise PublicationError("import authorization is not yet valid")
        if current >= _utc(self.expires_at):
            raise PublicationError("import authorization is expired")
        if (
            self.request_digest != request.digest
            or self.policy_digest != request.policy_digest
            or self.actor != request.actor
            or self.decision != "allow"
        ):
            raise PublicationError("import authorization does not match request")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "authorization_id": self.authorization_id,
            "request_digest": self.request_digest,
            "policy_digest": self.policy_digest,
            "actor": self.actor,
            "reason": self.reason,
            "decision": self.decision,
            "issued_at": _utc(self.issued_at).isoformat(),
            "expires_at": _utc(self.expires_at).isoformat(),
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ImportAuthorization:
        data = _strict_fields(
            _publication_document(value, cls.SCHEMA),
            _AUTHORIZATION_FIELDS | {"schema"},
            "import authorization",
        )
        return cls(
            authorization_id=_require_text(
                data["authorization_id"], "import authorization.authorization_id"
            ),
            request_digest=_require_text(
                data["request_digest"], "import authorization.request_digest"
            ),
            policy_digest=_require_text(
                data["policy_digest"], "import authorization.policy_digest"
            ),
            actor=_require_text(data["actor"], "import authorization.actor"),
            reason=_require_text(data["reason"], "import authorization.reason"),
            decision=_require_text(data["decision"], "import authorization.decision"),
            issued_at=_parse_datetime(
                data["issued_at"], "import authorization.issued_at"
            ),
            expires_at=_parse_datetime(
                data["expires_at"], "import authorization.expires_at"
            ),
            revoked=_require_boolean(data["revoked"], "import authorization.revoked"),
        )

    def _expected_id(self) -> str:
        values = self.to_dict()
        values.pop("schema")
        values.pop("authorization_id")
        values.pop("revoked")
        identity = _canonical_digest(values).removeprefix("sha256:")[:24]
        return f"import-auth:{identity}"


@dataclass(frozen=True, slots=True)
class ImportPolicy:
    """Current local trust policy for importing published artifacts."""

    policy_digest: str
    allowed_source_targets: tuple[str, ...]
    allowed_classification_digests: tuple[str, ...]
    trusted_publication_policy_digests: tuple[str, ...]
    allowed_importers: tuple[str, ...] = (_FILESYSTEM_IMPORTER_ID,)
    permitted_profiles: tuple[SecurityProfile, ...] = (
        SecurityProfile.CONSTRAINED,
        SecurityProfile.REVIEWED,
        SecurityProfile.PRIVILEGED_REVIEW,
    )
    maximum_authorization_lifetime: timedelta = timedelta(minutes=10)
    revoked_authorization_ids: tuple[str, ...] = ()
    revoked_publication_authorization_ids: tuple[str, ...] = ()
    revoked_classification_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_digest(self.policy_digest, "import policy")
        if (
            not self.allowed_source_targets
            or not self.allowed_classification_digests
            or not self.trusted_publication_policy_digests
            or not self.allowed_importers
            or not self.permitted_profiles
        ):
            raise ValueError("import policy requires explicit trust allowlists")
        for target in self.allowed_source_targets:
            _validate_id(target, "target")
        for values, label in (
            (self.allowed_classification_digests, "allowed classification"),
            (self.trusted_publication_policy_digests, "trusted publication policy"),
            (self.revoked_classification_digests, "revoked classification"),
        ):
            for value in values:
                _require_digest(value, label)
            if len(values) != len(set(values)):
                raise ValueError(f"import {label} identities must be unique")
        for values, label in (
            (self.allowed_source_targets, "source target"),
            (self.allowed_importers, "importer"),
            (self.revoked_authorization_ids, "authorization revocation"),
            (
                self.revoked_publication_authorization_ids,
                "publication authorization revocation",
            ),
        ):
            if any(not item.strip() for item in values) or len(values) != len(
                set(values)
            ):
                raise ValueError(f"import {label} identities must be unique")
        if any(
            not isinstance(item, SecurityProfile) for item in self.permitted_profiles
        ) or len(self.permitted_profiles) != len(set(self.permitted_profiles)):
            raise ValueError("import profiles must be unique security profiles")
        if self.maximum_authorization_lifetime <= timedelta(0):
            raise ValueError("import authorization lifetime must be positive")

    def authorize(
        self,
        request: ImportRequest,
        *,
        reason: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> ImportAuthorization:
        self._require_request_allowed(request)
        issued = _utc(issued_at)
        expires = _utc(expires_at)
        if (
            not reason.strip()
            or expires <= issued
            or expires - issued > self.maximum_authorization_lifetime
        ):
            raise PublicationError("import authorization parameters are invalid")
        return ImportAuthorization.issue(
            request,
            reason=reason,
            issued_at=issued,
            expires_at=expires,
        )

    def require_import_valid(
        self,
        request: ImportRequest,
        authorization: ImportAuthorization,
        *,
        source_target_id: str,
        source_target_identity_digest: str,
        importer_id: str,
        destination_identity_digest: str,
        now: datetime,
    ) -> None:
        if (
            source_target_id != request.source_target_id
            or source_target_identity_digest != request.source_target_identity_digest
            or importer_id != request.importer_id
            or destination_identity_digest != request.destination_identity_digest
        ):
            raise PublicationError("import request targets another destination")
        self._require_request_allowed(request)
        if authorization.authorization_id in self.revoked_authorization_ids:
            raise PublicationError("import authorization is revoked by policy")
        if (
            _utc(authorization.expires_at) - _utc(authorization.issued_at)
            > self.maximum_authorization_lifetime
        ):
            raise PublicationError("import authorization exceeds the policy lifetime")
        authorization.require_valid(request, now=now)

    def _require_request_allowed(self, request: ImportRequest) -> None:
        if request.policy_digest != self.policy_digest:
            raise PublicationError("import request targets another policy")
        if request.source_target_id not in self.allowed_source_targets:
            raise PublicationError("import source target is not permitted by policy")
        if request.importer_id not in self.allowed_importers:
            raise PublicationError("importer is not permitted by policy")
        if request.security_profile not in self.permitted_profiles:
            raise PublicationError("security profile is not importable by policy")
        if (
            request.security_classification_digest
            not in self.allowed_classification_digests
            or request.security_classification_digest
            in self.revoked_classification_digests
        ):
            raise PublicationError("security classification is not currently trusted")
        if (
            request.publication_policy_digest
            not in self.trusted_publication_policy_digests
        ):
            raise PublicationError("publication policy is not trusted for import")
        if (
            request.publication_authorization_id
            in self.revoked_publication_authorization_ids
        ):
            raise PublicationError("publication authorization is revoked for import")


@dataclass(frozen=True, slots=True)
class TransferReceipt:
    """Immutable completion receipt for one explicit publish or import."""

    SCHEMA: ClassVar[str] = TRANSFER_RECEIPT_SCHEMA

    operation_id: str
    direction: str
    target_id: str
    target_identity_digest: str
    publication_manifest: BlobRef
    component_id: str
    revision: str
    component_ref: ComponentRevisionRef
    component_lock_identity: ContentIdentity
    effective_revision_digest: str
    source_bundle: BlobRef
    provenance: tuple[BlobRef, ...]
    security_classification_digest: str
    security_profile: SecurityProfile
    publication_request_digest: str
    publication_authorization_id: str
    publication_policy_digest: str
    import_request_digest: str | None
    import_authorization_id: str | None
    import_policy_digest: str | None
    blob_count: int
    transferred_count: int
    reused_count: int
    started_at: str
    completed_at: str
    schema_version: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, tuple):
            raise ValueError("receipt provenance must be a tuple")
        _require_text(self.operation_id, "receipt.operation_id")
        if self.direction not in {"publish", "import"}:
            raise ValueError("receipt direction must be publish or import")
        _validate_id(self.target_id, "target")
        _require_digest(self.target_identity_digest, "publication target")
        _validate_id(self.component_id, "component")
        if (
            _require_integer(
                self.schema_version,
                "receipt.schema_version",
                minimum=1,
            )
            != 5
        ):
            raise ValueError("receipt requires schema v5")
        if not isinstance(self.component_ref, ComponentRevisionRef):
            raise ValueError("receipt requires an exact Component revision ref")
        if not isinstance(self.component_lock_identity, ContentIdentity):
            raise ValueError("receipt requires an exact Component lock identity")
        _require_text(self.revision, "receipt.revision")
        if (
            self.component_id != self.component_ref.coordinate.name
            or self.revision != self.component_ref.revision_identity.uri
        ):
            raise ValueError("receipt Component identity does not match exact ref")
        _validate_blob_ref(self.publication_manifest, "receipt.publication_manifest")
        _validate_blob_ref(self.source_bundle, "receipt.source_bundle")
        for value, label in (
            (self.effective_revision_digest, "effective revision"),
            (self.security_classification_digest, "security classification"),
            (self.publication_request_digest, "publication request"),
            (self.publication_policy_digest, "publication policy"),
        ):
            _require_digest(value, label)
        if not self.provenance:
            raise ValueError("receipt requires exact provenance")
        for index, item in enumerate(self.provenance):
            _validate_blob_ref(item, f"receipt.provenance[{index}]")
        provenance_identities = tuple(item.identity for item in self.provenance)
        if len(provenance_identities) != len(set(provenance_identities)):
            raise ValueError("receipt provenance must be unique")
        if not isinstance(self.security_profile, SecurityProfile):
            raise ValueError("receipt security profile is invalid")
        _require_text(
            self.publication_authorization_id,
            "receipt.publication_authorization_id",
        )
        import_values = (
            self.import_request_digest,
            self.import_authorization_id,
            self.import_policy_digest,
        )
        if self.direction == "publish" and any(
            item is not None for item in import_values
        ):
            raise ValueError("publish receipt must not claim import authorization")
        if self.direction == "import":
            if any(item is None for item in import_values):
                raise ValueError(
                    "import receipt requires local authorization provenance"
                )
            assert self.import_request_digest is not None
            assert self.import_policy_digest is not None
            _require_digest(self.import_request_digest, "import request")
            _require_digest(self.import_policy_digest, "import policy")
            _require_text(
                self.import_authorization_id,
                "receipt.import_authorization_id",
            )
        _require_integer(self.blob_count, "receipt.blob_count", minimum=1)
        _require_integer(
            self.transferred_count,
            "receipt.transferred_count",
        )
        _require_integer(self.reused_count, "receipt.reused_count")
        if self.blob_count < 1:
            raise ValueError("receipt must cover at least one blob")
        if self.transferred_count < 0 or self.reused_count < 0:
            raise ValueError("receipt counts must not be negative")
        if self.transferred_count + self.reused_count != self.blob_count:
            raise ValueError("receipt counts must account for every blob")
        started = _parse_datetime(self.started_at, "receipt.started_at")
        completed = _parse_datetime(self.completed_at, "receipt.completed_at")
        if _utc(completed) < _utc(started):
            raise ValueError("receipt completion precedes its start")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "direction": self.direction,
            "target_id": self.target_id,
            "target_identity_digest": self.target_identity_digest,
            "publication_manifest": self.publication_manifest.to_dict(),
            "component_id": self.component_id,
            "revision": self.revision,
            "component_ref": self.component_ref.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "effective_revision_digest": self.effective_revision_digest,
            "source_bundle": self.source_bundle.to_dict(),
            "provenance": [item.to_dict() for item in self.provenance],
            "security_classification_digest": self.security_classification_digest,
            "security_profile": self.security_profile.value,
            "publication_request_digest": self.publication_request_digest,
            "publication_authorization_id": self.publication_authorization_id,
            "publication_policy_digest": self.publication_policy_digest,
            "import_request_digest": self.import_request_digest,
            "import_authorization_id": self.import_authorization_id,
            "import_policy_digest": self.import_policy_digest,
            "blob_count": self.blob_count,
            "transferred_count": self.transferred_count,
            "reused_count": self.reused_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        legacy_manifest: PublicationManifest | None = None,
        legacy_manifest_document: Mapping[str, Any] | None = None,
    ) -> TransferReceipt:
        envelope = _strict_object(value, "transfer receipt")
        if "schema_version" not in envelope:
            raise ValueError("transfer receipt is missing field: schema_version")
        schema_version = _require_integer(
            envelope["schema_version"],
            "transfer receipt.schema_version",
            minimum=None,
        )
        if schema_version > 5:
            raise MigrationError(
                "contracts.migration_future_schema",
                f"cannot read future receipt schema {schema_version}",
            )
        if schema_version < 2:
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "receipt v1 lacks Component coordinate and semantic version",
            )
        if "schema" in envelope and schema_version == 5:
            envelope = normalize_publication_document(
                envelope, expected_schema=cls.SCHEMA
            )
        elif "schema" in envelope:
            if schema_version != 4 or envelope.get("schema") != cls.SCHEMA:
                raise MigrationError(
                    "contracts.migration_schema_unsupported",
                    "legacy receipt schema identity is unsupported",
                )
        elif schema_version == 5:
            raise MigrationError(
                "contracts.migration_schema_unsupported",
                "receipt v5 requires its v2 schema identity",
            )
        if schema_version == 2 and legacy_manifest is None:
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "receipt v2 lacks source, provenance, and policy decision",
            )
        if schema_version < 5 and (
            legacy_manifest is None or legacy_manifest_document is None
        ):
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "legacy receipt lacks an exact Component lock and verified "
                "publication manifest bytes",
            )
        data = _strict_fields(
            envelope,
            (
                _TRANSFER_RECEIPT_FIELDS | {"schema"}
                if schema_version == 5
                else _LEGACY_V4_TRANSFER_RECEIPT_FIELDS | {"schema"}
                if schema_version == 4
                else _LEGACY_V4_TRANSFER_RECEIPT_FIELDS
                if schema_version == 3
                else _LEGACY_RECEIPT_FIELDS
            ),
            (
                "transfer receipt"
                if schema_version == 5
                else "publication v4 transfer receipt"
                if schema_version == 4
                else "post-v0.1.1 transfer receipt"
                if schema_version == 3
                else "legacy transfer receipt"
            ),
        )
        component_ref = ComponentRevisionRef.from_dict(data["component_ref"])
        component_id = _require_text(
            data["component_id"], "transfer receipt.component_id"
        )
        revision = _require_text(data["revision"], "transfer receipt.revision")
        publication_manifest_ref = _parse_blob_ref(
            data["publication_manifest"], "transfer receipt.publication_manifest"
        )
        if legacy_manifest is not None and (
            component_ref != legacy_manifest.component_ref
            or component_id != legacy_manifest.component_id
            or revision != legacy_manifest.revision
        ):
            raise MigrationError(
                "contracts.migration_legacy_ambiguous",
                "legacy receipt does not match supplied publication manifest",
            )
        if legacy_manifest is not None:
            if schema_version < 5:
                assert legacy_manifest_document is not None
                migrated_context = PublicationManifest.from_dict(
                    legacy_manifest_document,
                    legacy_component_lock_identity=(
                        legacy_manifest.component_lock_identity
                    ),
                )
                if migrated_context != legacy_manifest:
                    raise MigrationError(
                        "contracts.migration_legacy_ambiguous",
                        "legacy receipt manifest bytes do not match supplied "
                        "migration context",
                    )
                manifest_bytes = canonical_json_bytes(legacy_manifest_document)
            else:
                manifest_bytes = canonical_json_bytes(legacy_manifest.to_dict())
            expected_manifest_ref = BlobRef(
                digest=hashlib.sha256(manifest_bytes).hexdigest(),
                size=len(manifest_bytes),
                media_type=_PUBLICATION_MANIFEST_MEDIA_TYPE,
            )
            if publication_manifest_ref != expected_manifest_ref:
                raise MigrationError(
                    "contracts.migration_legacy_ambiguous",
                    "legacy receipt does not bind the exact supplied publication "
                    "manifest bytes",
                )
        if schema_version in {3, 4, 5}:
            target_identity_digest = _require_text(
                data["target_identity_digest"],
                "transfer receipt.target_identity_digest",
            )
            effective_revision_digest = _require_text(
                data["effective_revision_digest"],
                "transfer receipt.effective_revision_digest",
            )
            component_lock_identity = (
                ContentIdentity.from_dict(
                    data["component_lock_identity"],
                    path="transfer receipt.component_lock_identity",
                )
                if schema_version == 5
                else legacy_manifest.component_lock_identity
            )
            source_bundle = _parse_blob_ref(
                data["source_bundle"], "transfer receipt.source_bundle"
            )
            provenance = _parse_blob_array(
                data["provenance"], "transfer receipt.provenance"
            )
            security_classification_digest = _require_text(
                data["security_classification_digest"],
                "transfer receipt.security_classification_digest",
            )
            security_profile = SecurityProfile(
                _require_text(
                    data["security_profile"],
                    "transfer receipt.security_profile",
                )
            )
            publication_request_digest = _require_text(
                data["publication_request_digest"],
                "transfer receipt.publication_request_digest",
            )
            publication_authorization_id = _require_text(
                data["publication_authorization_id"],
                "transfer receipt.publication_authorization_id",
            )
            publication_policy_digest = _require_text(
                data["publication_policy_digest"],
                "transfer receipt.publication_policy_digest",
            )
            import_request_digest = _optional_text(
                data["import_request_digest"],
                "transfer receipt.import_request_digest",
            )
            import_authorization_id = _optional_text(
                data["import_authorization_id"],
                "transfer receipt.import_authorization_id",
            )
            import_policy_digest = _optional_text(
                data["import_policy_digest"],
                "transfer receipt.import_policy_digest",
            )
        else:
            assert legacy_manifest is not None
            request = legacy_manifest.request
            target_identity_digest = request.target_identity_digest
            effective_revision_digest = request.effective_revision_digest
            component_lock_identity = request.component_lock_identity
            source_bundle = request.source_bundle
            provenance = request.provenance
            security_classification_digest = request.security_classification_digest
            security_profile = request.security_profile
            publication_request_digest = request.digest
            publication_authorization_id = (
                legacy_manifest.authorization.authorization_id
            )
            publication_policy_digest = request.policy_digest
            import_request_digest = None
            import_authorization_id = None
            import_policy_digest = None
        receipt = cls(
            schema_version=5,
            operation_id=_require_text(
                data["operation_id"], "transfer receipt.operation_id"
            ),
            direction=_require_text(data["direction"], "transfer receipt.direction"),
            target_id=_require_text(data["target_id"], "transfer receipt.target_id"),
            target_identity_digest=target_identity_digest,
            publication_manifest=publication_manifest_ref,
            component_id=component_id,
            revision=revision,
            component_ref=component_ref,
            component_lock_identity=component_lock_identity,
            effective_revision_digest=effective_revision_digest,
            source_bundle=source_bundle,
            provenance=provenance,
            security_classification_digest=security_classification_digest,
            security_profile=security_profile,
            publication_request_digest=publication_request_digest,
            publication_authorization_id=publication_authorization_id,
            publication_policy_digest=publication_policy_digest,
            import_request_digest=import_request_digest,
            import_authorization_id=import_authorization_id,
            import_policy_digest=import_policy_digest,
            blob_count=_require_integer(
                data["blob_count"], "transfer receipt.blob_count", minimum=1
            ),
            transferred_count=_require_integer(
                data["transferred_count"], "transfer receipt.transferred_count"
            ),
            reused_count=_require_integer(
                data["reused_count"], "transfer receipt.reused_count"
            ),
            started_at=_require_text(data["started_at"], "transfer receipt.started_at"),
            completed_at=_require_text(
                data["completed_at"], "transfer receipt.completed_at"
            ),
        )
        if legacy_manifest is not None and (
            receipt.target_identity_digest
            != legacy_manifest.request.target_identity_digest
            or receipt.component_lock_identity
            != legacy_manifest.request.component_lock_identity
            or receipt.effective_revision_digest
            != legacy_manifest.request.effective_revision_digest
            or receipt.source_bundle != legacy_manifest.request.source_bundle
            or receipt.provenance != legacy_manifest.request.provenance
            or receipt.security_classification_digest
            != legacy_manifest.request.security_classification_digest
            or receipt.security_profile != legacy_manifest.request.security_profile
            or receipt.publication_request_digest != legacy_manifest.request.digest
            or receipt.publication_authorization_id
            != legacy_manifest.authorization.authorization_id
            or receipt.publication_policy_digest
            != legacy_manifest.request.policy_digest
        ):
            raise ValueError("transfer receipt provenance does not match manifest")
        return receipt


class FilesystemPublicationTarget:
    """Immutable blob transport rooted in one dedicated directory."""

    def __init__(self, target_id: str, root: str | Path) -> None:
        _validate_id(target_id, "target")
        configured = Path(root).expanduser()
        if not configured.is_absolute():
            raise StorageSafetyError("publication target must be an absolute path")
        if configured.is_symlink():
            raise StorageSafetyError("publication target must not be a symbolic link")
        configured.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.target_id = target_id
        self.root = configured.resolve(strict=True)
        if self.root == Path(self.root.anchor) or not self.root.is_dir():
            raise StorageSafetyError("publication target must be a dedicated directory")
        self.blob_root = self.root / "blobs" / "sha256"
        self.release_root = self.root / "releases"
        self.blob_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.release_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._safe_directory(self.blob_root)
        self._safe_directory(self.release_root)

    @property
    def identity(self) -> str:
        return _canonical_digest(
            {
                "publisher_id": _FILESYSTEM_PUBLISHER_ID,
                "target_id": self.target_id,
                "root": str(self.root),
            }
        )

    def has_blob(self, reference: BlobRef) -> bool:
        path = self.path_for(reference)
        if path.is_symlink() or not path.is_file():
            return False
        try:
            self.verify_blob(reference)
        except (BlobNotFoundError, BlobIntegrityError):
            return False
        return True

    def verify_blob(self, reference: BlobRef) -> None:
        path = self.path_for(reference)
        if path.is_symlink() or not path.is_file():
            raise BlobNotFoundError(f"published blob {reference.identity} is missing")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(_BUFFER_SIZE):
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != reference.digest or size != reference.size:
            raise BlobIntegrityError(
                f"published blob {reference.identity} failed verification"
            )

    def put_blob(self, source: FileSystemCAS, reference: BlobRef) -> bool:
        """Transfer one blob; return True only when bytes were newly copied."""

        source.verify(reference)
        destination = self.path_for(reference)
        prefix = destination.parent
        if prefix.exists() and prefix.is_symlink():
            raise StorageSafetyError("publication prefix must not be symbolic")
        prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._safe_directory(prefix)
        if destination.exists() or destination.is_symlink():
            self.verify_blob(reference)
            return False
        fd, temp_name = tempfile.mkstemp(prefix=".publish-", dir=prefix)
        temp_path = Path(temp_name)
        try:
            with (
                source.path_for(reference).open("rb") as input_file,
                os.fdopen(fd, "wb") as output,
            ):
                while chunk := input_file.read(_BUFFER_SIZE):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_path, 0o600)
            if _file_ref(temp_path, reference.media_type) != reference:
                raise BlobIntegrityError("publication copy failed verification")
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                self.verify_blob(reference)
                return False
            return True
        finally:
            temp_path.unlink(missing_ok=True)

    def import_blob(self, destination: FileSystemCAS, reference: BlobRef) -> bool:
        """Import one verified blob; return True only when it was absent locally."""

        self.verify_blob(reference)
        existed = destination.contains(reference)
        imported = destination.put_file(
            self.path_for(reference), media_type=reference.media_type
        )
        if imported != reference:
            raise BlobIntegrityError("imported blob identity changed")
        return not existed

    def record_release(
        self,
        component_id: str,
        publication_manifest: BlobRef,
    ) -> Path:
        _validate_id(component_id, "component")
        component_root = self.release_root / component_id
        if component_root.exists() and component_root.is_symlink():
            raise StorageSafetyError("release directory must not be symbolic")
        component_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._safe_directory(component_root)
        destination = component_root / f"{publication_manifest.digest}.json"
        payload = (
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "publication_manifest": publication_manifest.to_dict(),
                }
            )
            + b"\n"
        )
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or destination.read_bytes() != payload:
                raise PublicationError("immutable release record conflicts")
            return destination
        fd, temp_name = tempfile.mkstemp(prefix=".release-", dir=component_root)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_path, 0o600)
            try:
                os.link(temp_path, destination)
            except FileExistsError as exc:
                if destination.is_symlink() or destination.read_bytes() != payload:
                    raise PublicationError(
                        "immutable release record conflicts"
                    ) from exc
            return destination
        finally:
            temp_path.unlink(missing_ok=True)

    def publication_manifest_ref(self, component_id: str, digest: str) -> BlobRef:
        _validate_id(component_id, "component")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("release digest is invalid")
        path = self.release_root / component_id / f"{digest}.json"
        if path.is_symlink() or not path.is_file():
            raise PublicationError("release record is missing")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            reference = BlobRef.from_dict(raw["publication_manifest"])
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ) as exc:
            raise PublicationError("release record is invalid") from exc
        if reference.digest != digest:
            raise PublicationError("release record digest does not match its filename")
        self.verify_blob(reference)
        return reference

    def path_for(self, reference: BlobRef) -> Path:
        path = self.blob_root / reference.digest[:2] / reference.digest
        if not path.parent.resolve().is_relative_to(self.blob_root):
            raise StorageSafetyError("publication blob path escaped its root")
        return path

    def _safe_directory(self, path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise StorageSafetyError("publication path must be a regular directory")
        resolved = path.resolve(strict=True)
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise StorageSafetyError("publication path escaped its root")


class PublicationService:
    """Publish and import immutable blobs with append-only resumable state."""

    def __init__(
        self,
        cas: FileSystemCAS,
        events: AppendOnlyEventStore,
        policy: PublicationPolicy | None = None,
        import_policy: ImportPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cas = cas
        self.events = events
        self.policy = policy
        self.import_policy = import_policy
        self.clock = clock

    @property
    def import_destination_identity(self) -> str:
        return _canonical_digest(
            {
                "importer_id": _FILESYSTEM_IMPORTER_ID,
                "cas_root": str(self.cas.root.resolve()),
                "event_root": str(self.events.root.resolve()),
            }
        )

    def publish(
        self,
        manifest: PublicationManifest,
        target: FilesystemPublicationTarget,
    ) -> TransferReceipt:
        if self.policy is None:
            raise PublicationError("publication requires an explicit policy")
        self.policy.require_publish_valid(
            manifest.request,
            manifest.authorization,
            target_id=target.target_id,
            target_identity_digest=target.identity,
            publisher_id=_FILESYSTEM_PUBLISHER_ID,
            now=self.clock(),
        )
        self._require_non_overlapping(target)
        for reference in manifest.blobs:
            self.cas.verify(reference)
        manifest_ref = self.cas.put_manifest(
            manifest.to_dict(),
            media_type=_PUBLICATION_MANIFEST_MEDIA_TYPE,
        )
        references = (*manifest.blobs, manifest_ref)
        operation_id = self._operation_id("publish", target, manifest_ref)
        stream = f"publication:{operation_id}"
        completed = self._completed_receipt(stream, manifest, manifest_ref)
        if completed is not None:
            return completed
        started_at = self._ensure_started(
            stream, "publish", target, manifest_ref, manifest
        )
        try:
            for reference in references:
                transferred = target.put_blob(self.cas, reference)
                self.events.append(
                    stream,
                    "blob.transferred" if transferred else "blob.reused",
                    {"blob": reference.to_dict()},
                )
            target.record_release(manifest.component_id, manifest_ref)
            return self._complete(
                stream=stream,
                operation_id=operation_id,
                direction="publish",
                target=target,
                manifest_ref=manifest_ref,
                manifest=manifest,
                references=references,
                started_at=started_at,
            )
        except Exception as exc:
            self.events.append(
                stream,
                "transfer.interrupted",
                {"error_type": _stable_transfer_failure_type(exc)},
            )
            raise

    def import_release(
        self,
        publication_manifest: BlobRef,
        target: FilesystemPublicationTarget,
        *,
        expected_component: ComponentRevisionRef,
        import_authorization: ImportAuthorization | None = None,
        legacy_request: PublicationRequest | None = None,
        legacy_authorization: PublicationAuthorization | None = None,
        legacy_component_lock_identity: ContentIdentity | None = None,
    ) -> TransferReceipt:
        if self.import_policy is None or import_authorization is None:
            raise PublicationError(
                "import requires an explicit local policy and authorization"
            )
        self._require_non_overlapping(target)
        target.verify_blob(publication_manifest)
        try:
            raw = json.loads(target.path_for(publication_manifest).read_bytes())
            manifest = PublicationManifest.from_dict(
                raw,
                legacy_component_ref=expected_component,
                legacy_request=legacy_request,
                legacy_authorization=legacy_authorization,
                legacy_component_lock_identity=legacy_component_lock_identity,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise PublicationError("published manifest is invalid") from exc
        if manifest.component_ref != expected_component:
            raise PublicationError(
                "published manifest does not match the expected Component revision"
            )
        if (
            manifest.request.target_id != target.target_id
            or manifest.request.target_identity_digest != target.identity
            or manifest.request.publisher_id != _FILESYSTEM_PUBLISHER_ID
        ):
            raise PublicationError("published manifest targets another publisher")
        import_request = ImportRequest.create(
            manifest=manifest,
            publication_manifest=publication_manifest,
            destination_identity_digest=self.import_destination_identity,
            policy_digest=self.import_policy.policy_digest,
            actor=import_authorization.actor,
        )
        self.import_policy.require_import_valid(
            import_request,
            import_authorization,
            source_target_id=target.target_id,
            source_target_identity_digest=target.identity,
            importer_id=_FILESYSTEM_IMPORTER_ID,
            destination_identity_digest=self.import_destination_identity,
            now=self.clock(),
        )
        references = (*manifest.blobs, publication_manifest)
        operation_id = self._operation_id(
            "import",
            target,
            publication_manifest,
            request_digest=import_request.digest,
        )
        stream = f"publication:{operation_id}"
        completed = self._completed_receipt(
            stream,
            manifest,
            publication_manifest,
            import_request=import_request,
            import_authorization=import_authorization,
        )
        if completed is not None:
            return completed
        started_at = self._ensure_started(
            stream,
            "import",
            target,
            publication_manifest,
            manifest,
            import_request=import_request,
            import_authorization=import_authorization,
        )
        try:
            for reference in references:
                transferred = target.import_blob(self.cas, reference)
                self.events.append(
                    stream,
                    "blob.transferred" if transferred else "blob.reused",
                    {"blob": reference.to_dict()},
                )
            return self._complete(
                stream=stream,
                operation_id=operation_id,
                direction="import",
                target=target,
                manifest_ref=publication_manifest,
                manifest=manifest,
                references=references,
                started_at=started_at,
                import_request=import_request,
                import_authorization=import_authorization,
            )
        except Exception as exc:
            self.events.append(
                stream,
                "transfer.interrupted",
                {"error_type": _stable_transfer_failure_type(exc)},
            )
            raise

    def _complete(
        self,
        *,
        stream: str,
        operation_id: str,
        direction: str,
        target: FilesystemPublicationTarget,
        manifest_ref: BlobRef,
        manifest: PublicationManifest,
        references: tuple[BlobRef, ...],
        started_at: str,
        import_request: ImportRequest | None = None,
        import_authorization: ImportAuthorization | None = None,
    ) -> TransferReceipt:
        events = self.events.read(stream)
        transferred_identities = {
            str(item.data["blob"]["algorithm"]) + ":" + str(item.data["blob"]["digest"])
            for item in events
            if item.event_type == "blob.transferred"
        }
        all_identities = {item.identity for item in references}
        receipt = TransferReceipt(
            operation_id=operation_id,
            direction=direction,
            target_id=target.target_id,
            target_identity_digest=target.identity,
            publication_manifest=manifest_ref,
            component_id=manifest.component_id,
            revision=manifest.revision,
            component_ref=manifest.component_ref,
            component_lock_identity=manifest.component_lock_identity,
            effective_revision_digest=(manifest.request.effective_revision_digest),
            source_bundle=manifest.request.source_bundle,
            provenance=manifest.request.provenance,
            security_classification_digest=(
                manifest.request.security_classification_digest
            ),
            security_profile=manifest.request.security_profile,
            publication_request_digest=manifest.request.digest,
            publication_authorization_id=(manifest.authorization.authorization_id),
            publication_policy_digest=manifest.request.policy_digest,
            import_request_digest=(
                import_request.digest if import_request is not None else None
            ),
            import_authorization_id=(
                import_authorization.authorization_id
                if import_authorization is not None
                else None
            ),
            import_policy_digest=(
                import_request.policy_digest if import_request is not None else None
            ),
            blob_count=len(all_identities),
            transferred_count=len(transferred_identities & all_identities),
            reused_count=len(all_identities - transferred_identities),
            started_at=started_at,
            completed_at=self.clock().isoformat(),
        )
        receipt_ref = self.cas.put_manifest(
            receipt.to_dict(),
            media_type="application/vnd.literate-ai.transfer-receipt+json",
        )
        self.events.append(
            stream,
            "transfer.completed",
            {"receipt": receipt_ref.to_dict()},
        )
        return receipt

    def _completed_receipt(
        self,
        stream: str,
        manifest: PublicationManifest,
        manifest_ref: BlobRef,
        *,
        import_request: ImportRequest | None = None,
        import_authorization: ImportAuthorization | None = None,
    ) -> TransferReceipt | None:
        completed = [
            item
            for item in self.events.read(stream)
            if item.event_type == "transfer.completed"
        ]
        if not completed:
            return None
        try:
            reference = BlobRef.from_dict(completed[-1].data["receipt"])
            receipt = TransferReceipt.from_dict(
                self.cas.get_manifest(reference),
                legacy_manifest=manifest,
            )
            if receipt.publication_manifest != manifest_ref:
                raise PublicationError(
                    "completed receipt targets another publication manifest"
                )
            if import_request is not None and (
                receipt.import_request_digest != import_request.digest
                or import_authorization is None
                or receipt.import_authorization_id
                != import_authorization.authorization_id
                or receipt.import_policy_digest != import_request.policy_digest
            ):
                raise PublicationError(
                    "completed receipt targets another import authorization"
                )
            return receipt
        except (KeyError, ValueError, StorageError) as exc:
            raise PublicationError("completed transfer receipt is invalid") from exc

    def _ensure_started(
        self,
        stream: str,
        direction: str,
        target: FilesystemPublicationTarget,
        manifest_ref: BlobRef,
        manifest: PublicationManifest,
        *,
        import_request: ImportRequest | None = None,
        import_authorization: ImportAuthorization | None = None,
    ) -> str:
        events = self.events.read(stream)
        started = [item for item in events if item.event_type == "transfer.started"]
        if started:
            return str(started[0].data["started_at"])
        started_at = self.clock().isoformat()
        self.events.append(
            stream,
            "transfer.started",
            {
                "direction": direction,
                "target_id": target.target_id,
                "target_identity_digest": target.identity,
                "publication_manifest": manifest_ref.to_dict(),
                "component_id": manifest.component_id,
                "revision": manifest.revision,
                "component_ref": manifest.component_ref.to_dict(),
                "effective_revision_digest": (
                    manifest.request.effective_revision_digest
                ),
                "source_bundle": manifest.request.source_bundle.to_dict(),
                "provenance": [item.to_dict() for item in manifest.request.provenance],
                "security_classification_digest": (
                    manifest.request.security_classification_digest
                ),
                "security_profile": manifest.request.security_profile.value,
                "publication_request_digest": manifest.request.digest,
                "publication_authorization_id": (
                    manifest.authorization.authorization_id
                ),
                "publication_policy_digest": manifest.request.policy_digest,
                "import_request_digest": (
                    import_request.digest if import_request is not None else None
                ),
                "import_authorization_id": (
                    import_authorization.authorization_id
                    if import_authorization is not None
                    else None
                ),
                "import_policy_digest": (
                    import_request.policy_digest if import_request is not None else None
                ),
                "started_at": started_at,
            },
        )
        return started_at

    def _require_non_overlapping(self, target: FilesystemPublicationTarget) -> None:
        cas_root = self.cas.root.resolve()
        event_root = self.events.root.resolve()
        target_root = target.root.resolve()
        overlaps_cas = (
            cas_root == target_root
            or cas_root.is_relative_to(target_root)
            or target_root.is_relative_to(cas_root)
        )
        overlaps_events = (
            event_root == target_root
            or event_root.is_relative_to(target_root)
            or target_root.is_relative_to(event_root)
        )
        if overlaps_cas or overlaps_events:
            raise StorageSafetyError("publication target must not overlap local state")

    @staticmethod
    def _operation_id(
        direction: str,
        target: FilesystemPublicationTarget,
        manifest_ref: BlobRef,
        *,
        request_digest: str | None = None,
    ) -> str:
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "direction": direction,
                    "target_id": target.target_id,
                    "target_root": str(target.root),
                    "publication_manifest": manifest_ref.to_dict(),
                    "request_digest": request_digest,
                }
            )
        ).hexdigest()
        return f"{direction}_{digest[:24]}"


def _strict_object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} object keys must be strings")
    return value


def _strict_fields(
    value: Any,
    required: frozenset[str],
    path: str,
) -> Mapping[str, Any]:
    data = _strict_object(value, path)
    keys = frozenset(data)
    missing = required - keys
    if missing:
        raise ValueError(f"{path} is missing fields: {', '.join(sorted(missing))}")
    unexpected = keys - required
    if unexpected:
        raise ValueError(
            f"{path} has unexpected fields: {', '.join(sorted(unexpected))}"
        )
    return data


def _require_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _require_integer(
    value: Any,
    path: str,
    *,
    minimum: int | None = 0,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{path} must be at least {minimum}")
    return value


def _require_boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _require_array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    return value


def _optional_text(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, path)


def _parse_datetime(value: Any, path: str) -> datetime:
    raw = _require_text(value, path)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{path} is not an ISO-8601 timestamp") from exc
    _utc(parsed)
    return parsed


def _validate_blob_ref(value: Any, path: str) -> BlobRef:
    if not isinstance(value, BlobRef):
        raise ValueError(f"{path} must be a BlobRef")
    _require_text(value.algorithm, f"{path}.algorithm")
    _require_text(value.digest, f"{path}.digest")
    _require_integer(value.size, f"{path}.size")
    _require_text(value.media_type, f"{path}.media_type")
    return value


def _parse_blob_ref(value: Any, path: str) -> BlobRef:
    data = _strict_fields(
        value,
        frozenset({"algorithm", "digest", "size", "media_type"}),
        path,
    )
    return BlobRef(
        algorithm=_require_text(data["algorithm"], f"{path}.algorithm"),
        digest=_require_text(data["digest"], f"{path}.digest"),
        size=_require_integer(data["size"], f"{path}.size"),
        media_type=_require_text(data["media_type"], f"{path}.media_type"),
    )


def _parse_blob_array(
    value: Any,
    path: str,
    *,
    unique: bool = True,
) -> tuple[BlobRef, ...]:
    result = tuple(
        _parse_blob_ref(item, f"{path}[{index}]")
        for index, item in enumerate(_require_array(value, path))
    )
    if unique:
        identities = tuple(item.identity for item in result)
        if len(identities) != len(set(identities)):
            raise ValueError(f"{path} must contain unique blob identities")
    return result


def _parse_blob_roots(value: Any, path: str) -> dict[str, BlobRef]:
    roots = _strict_object(value, path)
    return {key: _parse_blob_ref(item, f"{path}.{key}") for key, item in roots.items()}


def _validate_id(value: str, kind: str) -> None:
    _require_text(value, f"publication {kind} ID")
    if not _SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"unsafe publication {kind} ID {value!r}")


def _require_digest(value: str, label: str) -> None:
    _require_text(value, f"publication {label}")
    if not _DIGEST_URI.fullmatch(value):
        raise ValueError(f"publication {label} must be an exact sha256 digest")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("publication timestamps must be datetime values")
    if value.tzinfo is None:
        raise ValueError("publication timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _validate_blob_closure(
    roots: Mapping[str, BlobRef],
    blobs: tuple[BlobRef, ...],
) -> None:
    if not roots or not blobs:
        raise ValueError("publication requires roots and blobs")
    for index, item in enumerate(blobs):
        _validate_blob_ref(item, f"publication.blobs[{index}]")
    identities = [item.identity for item in blobs]
    if len(identities) != len(set(identities)):
        raise ValueError("publication contains duplicate blobs")
    declared = set(identities)
    for name, reference in roots.items():
        _validate_id(name, "root")
        _validate_blob_ref(reference, f"publication.roots.{name}")
        if reference.identity not in declared:
            raise ValueError(f"publication root {name!r} is not in the blob set")


def _file_ref(path: Path, media_type: str) -> BlobRef:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_BUFFER_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return BlobRef(digest=digest.hexdigest(), size=size, media_type=media_type)
