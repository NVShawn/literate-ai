"""Explicit pinned-key evidence trust policy and independent verification inputs.

Issuer/key associations come from trusted configuration, never envelope hints or
producer claims. These records do not validate OIDC tokens or CI certificates.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from literate_ai.contracts._validation import (
    contract_fields,
    int_value,
    list_value,
    string_value,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from .records import (
    PREDICATE_TYPES,
    EvidenceArtifact,
    EvidenceLocator,
    EvidenceMatrix,
    EvidenceRunContext,
)

_PREFIX = "urn:literate-ai:schema:v2:"
_MAX_ITEMS = 1024


def _text(value: Any) -> str:
    result = string_value(value, "evidence.trust.identifier", max_length=1024)
    result.encode("utf-8")
    if result.strip() != result or any(ord(c) < 32 for c in result):
        raise ValueError("trust identifier must not contain control or edge whitespace")
    return result


def _strings(value: Any, *, empty: bool = False) -> None:
    if (
        not isinstance(value, tuple)
        or not (0 if empty else 1) <= len(value) <= _MAX_ITEMS
    ):
        raise ValueError("trust identifiers must be a bounded immutable tuple")
    for item in value:
        _text(item)
    if value != tuple(sorted(set(value))):
        raise ValueError("trust identifiers must be unique and sorted")


def _identities(value: Any, *, empty: bool = False) -> None:
    if (
        not isinstance(value, tuple)
        or not (0 if empty else 1) <= len(value) <= _MAX_ITEMS
    ):
        raise ValueError("trust identities must be a bounded immutable tuple")
    if any(not isinstance(item, ContentIdentity) for item in value):
        raise ValueError("trust requires immutable content identities")
    _strings(tuple(item.uri for item in value), empty=empty)


def _names(value: Any, *, empty: bool = False) -> None:
    _strings(value, empty=empty)
    if any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item) for item in value
    ):
        raise ValueError("store and invocation IDs must be portable names")


def _array(value: Any) -> tuple[Any, ...]:
    values = list_value(value, "evidence.trust.items")
    if len(values) > _MAX_ITEMS:
        raise ValueError("trust collection exceeds its bound")
    return tuple(values)


def _parse_identities(value: Any) -> tuple[ContentIdentity, ...]:
    return tuple(ContentIdentity.from_dict(item) for item in _array(value))


@dataclass(frozen=True, slots=True)
class EvidenceSignerRule:
    public_key: bytes
    issuer: str
    repositories: tuple[str, ...]
    workflows: tuple[ContentIdentity, ...]
    targets: tuple[ContentIdentity, ...]
    predicate_types: tuple[str, ...]
    store_ids: tuple[str, ...]
    valid_from: int
    valid_until: int

    SCHEMA: ClassVar[str] = f"{_PREFIX}evidence-signer-rule"

    def __post_init__(self):
        if not isinstance(self.public_key, bytes) or len(self.public_key) != 32:
            raise ValueError("evidence signer requires a raw Ed25519 public key")
        _text(self.issuer)
        _strings(self.repositories)
        _identities(self.workflows)
        _identities(self.targets)
        _strings(self.predicate_types)
        if not set(self.predicate_types).issubset(PREDICATE_TYPES):
            raise ValueError("signer policy names an unsupported predicate")
        _names(self.store_ids, empty=True)
        if (EvidenceLocator.SCHEMA in self.predicate_types) != bool(self.store_ids):
            raise ValueError("locator signing requires explicit store scope")
        int_value(self.valid_from, "evidence.signer.valid_from")
        int_value(
            self.valid_until, "evidence.signer.valid_until", minimum=self.valid_from + 1
        )

    @property
    def key_identity(self) -> ContentIdentity:
        return ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(self.public_key).hexdigest()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "public_key": self.public_key.hex(),
            "issuer": self.issuer,
            "repositories": list(self.repositories),
            "workflows": [v.to_dict() for v in self.workflows],
            "targets": [v.to_dict() for v in self.targets],
            "predicate_types": list(self.predicate_types),
            "store_ids": list(self.store_ids),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
        }

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceSignerRule:
        d = contract_fields(
            value,
            path="EvidenceSignerRule",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "public_key",
                    "issuer",
                    "repositories",
                    "workflows",
                    "targets",
                    "predicate_types",
                    "store_ids",
                    "valid_from",
                    "valid_until",
                }
            ),
        )
        key = d["public_key"]
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("signer public key must use canonical lowercase hex")
        return cls(
            bytes.fromhex(key),
            d["issuer"],
            _array(d["repositories"]),
            _parse_identities(d["workflows"]),
            _parse_identities(d["targets"]),
            _array(d["predicate_types"]),
            _array(d["store_ids"]),
            d["valid_from"],
            d["valid_until"],
        )


@dataclass(frozen=True, slots=True)
class EvidenceRevocations:
    issued_at: int
    expires_at: int
    key_identities: tuple[ContentIdentity, ...]
    issuers: tuple[str, ...]
    invocation_ids: tuple[str, ...]

    SCHEMA: ClassVar[str] = f"{_PREFIX}evidence-revocations"

    def __post_init__(self):
        int_value(self.issued_at, "evidence.revocations.issued_at")
        int_value(
            self.expires_at,
            "evidence.revocations.expires_at",
            minimum=self.issued_at + 1,
        )
        _identities(self.key_identities, empty=True)
        _strings(self.issuers, empty=True)
        _names(self.invocation_ids, empty=True)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_identities": [v.to_dict() for v in self.key_identities],
            "issuers": list(self.issuers),
            "invocation_ids": list(self.invocation_ids),
        }

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceRevocations:
        d = contract_fields(
            value,
            path="EvidenceRevocations",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "issued_at",
                    "expires_at",
                    "key_identities",
                    "issuers",
                    "invocation_ids",
                }
            ),
        )
        return cls(
            d["issued_at"],
            d["expires_at"],
            _parse_identities(d["key_identities"]),
            _array(d["issuers"]),
            _array(d["invocation_ids"]),
        )


@dataclass(frozen=True, slots=True)
class EvidenceTrustPolicy:
    signers: tuple[EvidenceSignerRule, ...]
    minimum_signatures: int
    maximum_run_age_seconds: int
    maximum_run_duration_seconds: int
    maximum_clock_skew_seconds: int
    maximum_revocation_age_seconds: int
    minimum_retention_seconds: int

    SCHEMA: ClassVar[str] = f"{_PREFIX}evidence-trust-policy"

    def __post_init__(self):
        if (
            not isinstance(self.signers, tuple)
            or not 1 <= len(self.signers) <= 64
            or any(not isinstance(v, EvidenceSignerRule) for v in self.signers)
        ):
            raise ValueError("trust policy requires 1 to 64 immutable signer rules")
        identities = tuple(rule.key_identity.uri for rule in self.signers)
        if identities != tuple(sorted(set(identities))):
            raise ValueError(
                "signer keys must be unique and sorted; "
                "issuer aliases cannot duplicate a key"
            )
        int_value(
            self.minimum_signatures,
            "evidence.trust.minimum_signatures",
            minimum=1,
            maximum=len(self.signers),
        )
        for name in (
            "maximum_run_age_seconds",
            "maximum_run_duration_seconds",
            "maximum_revocation_age_seconds",
        ):
            int_value(getattr(self, name), f"evidence.trust.{name}", minimum=1)
        int_value(
            self.maximum_clock_skew_seconds, "evidence.trust.maximum_clock_skew_seconds"
        )
        int_value(
            self.minimum_retention_seconds,
            "evidence.trust.minimum_retention_seconds",
            minimum=1,
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "signers": [v.to_dict() for v in self.signers],
            "minimum_signatures": self.minimum_signatures,
            "maximum_run_age_seconds": self.maximum_run_age_seconds,
            "maximum_run_duration_seconds": self.maximum_run_duration_seconds,
            "maximum_clock_skew_seconds": self.maximum_clock_skew_seconds,
            "maximum_revocation_age_seconds": self.maximum_revocation_age_seconds,
            "minimum_retention_seconds": self.minimum_retention_seconds,
        }

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceTrustPolicy:
        d = contract_fields(
            value,
            path="EvidenceTrustPolicy",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "signers",
                    "minimum_signatures",
                    "maximum_run_age_seconds",
                    "maximum_run_duration_seconds",
                    "maximum_clock_skew_seconds",
                    "maximum_revocation_age_seconds",
                    "minimum_retention_seconds",
                }
            ),
        )
        signers = _array(d["signers"])
        if len(signers) > 64:
            raise ValueError("too many evidence signer rules")
        return cls(
            tuple(EvidenceSignerRule.from_dict(v) for v in signers),
            d["minimum_signatures"],
            d["maximum_run_age_seconds"],
            d["maximum_run_duration_seconds"],
            d["maximum_clock_skew_seconds"],
            d["maximum_revocation_age_seconds"],
            d["minimum_retention_seconds"],
        )


@dataclass(frozen=True, slots=True)
class RunEvidenceExpectation:
    predicate_type: str
    subject: BlobRef
    invocation_id: str
    repository: str
    revision: str
    workflow: ContentIdentity
    target: ContentIdentity
    earliest_start: int
    latest_finish: int
    required_cells: tuple[str, ...]

    SCHEMA: ClassVar[str] = f"{_PREFIX}run-evidence-expectation"

    def __post_init__(self):
        if (
            not isinstance(self.predicate_type, str)
            or self.predicate_type not in PREDICATE_TYPES
            or self.predicate_type == EvidenceLocator.SCHEMA
        ):
            raise ValueError("expectation must select a supported run predicate")
        EvidenceArtifact("subject", self.subject)
        EvidenceRunContext(
            self.invocation_id,
            self.repository,
            self.revision,
            self.workflow,
            self.target,
            self.earliest_start,
            self.latest_finish,
        )
        _strings(self.required_cells, empty=True)
        for name in self.required_cells:
            EvidenceArtifact(name, self.subject)
        if (self.predicate_type == EvidenceMatrix.SCHEMA) != bool(self.required_cells):
            raise ValueError(
                "only matrix expectations require an explicit nonempty cell set"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "predicate_type": self.predicate_type,
            "subject": self.subject.to_dict(),
            "invocation_id": self.invocation_id,
            "repository": self.repository,
            "revision": self.revision,
            "workflow": self.workflow.to_dict(),
            "target": self.target.to_dict(),
            "earliest_start": self.earliest_start,
            "latest_finish": self.latest_finish,
            "required_cells": list(self.required_cells),
        }

    @classmethod
    def from_dict(cls, value: Any) -> RunEvidenceExpectation:
        d = contract_fields(
            value,
            path="RunEvidenceExpectation",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "predicate_type",
                    "subject",
                    "invocation_id",
                    "repository",
                    "revision",
                    "workflow",
                    "target",
                    "earliest_start",
                    "latest_finish",
                    "required_cells",
                }
            ),
        )
        return cls(
            d["predicate_type"],
            BlobRef.from_dict(d["subject"]),
            d["invocation_id"],
            d["repository"],
            d["revision"],
            ContentIdentity.from_dict(d["workflow"]),
            ContentIdentity.from_dict(d["target"]),
            d["earliest_start"],
            d["latest_finish"],
            _array(d["required_cells"]),
        )
