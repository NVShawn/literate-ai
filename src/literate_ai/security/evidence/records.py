"""Closed evidence predicates. Structural validity is never run admission.

Times are UTC Unix seconds. Repository and invocation identifiers are exact opaque
identifiers, not fetch instructions. Locators name configured stores; their signed
retention claims must still be checked against the store and current trust policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar

from literate_ai.contracts._validation import (
    contract_fields,
    fields,
    int_value,
    list_value,
    string_value,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity

DSSE_MEDIA_TYPE = "application/vnd.dsse.envelope.v1+json"
_PREFIX = "urn:literate-ai:schema:v2:"
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
MAXIMUM_EVIDENCE_ITEMS = 1024
MAXIMUM_EVIDENCE_BLOB_BYTES = 2**63 - 1


def _name(value: Any) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("evidence name must be a bounded portable identifier")
    return value


def _blob(value: Any, *, envelope: bool = False) -> BlobRef:
    if not isinstance(value, BlobRef):
        raise ValueError("evidence must reference immutable bytes")
    int_value(value.size, "evidence.size", maximum=MAXIMUM_EVIDENCE_BLOB_BYTES)
    media = string_value(value.media_type, "evidence.media_type", max_length=256)
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", media
    ):
        raise ValueError("evidence media type must be canonical without parameters")
    if envelope and media != DSSE_MEDIA_TYPE:
        raise ValueError("run references must identify DSSE envelope media")
    return value


def _context(value: Any) -> None:
    if not isinstance(value, EvidenceRunContext):
        raise ValueError("evidence run context is required")


def _status(value: Any) -> None:
    if not isinstance(value, str) or value not in ("passed", "failed", "incomplete"):
        raise ValueError("evidence status must be passed, failed or incomplete")


def _items(value: Any, *, envelope: bool = False) -> None:
    if (
        not isinstance(value, tuple)
        or not 1 <= len(value) <= MAXIMUM_EVIDENCE_ITEMS
        or any(not isinstance(item, EvidenceArtifact) for item in value)
    ):
        raise ValueError("evidence artifacts must be a bounded nonempty tuple")
    names = tuple(item.name for item in value)
    if names != tuple(sorted(set(names))):
        raise ValueError("evidence artifact names must be unique and sorted")
    for item in value:
        _blob(item.blob, envelope=envelope)


def _parse_items(value: Any) -> tuple[EvidenceArtifact, ...]:
    values = list_value(value, "evidence.artifacts")
    if not 1 <= len(values) <= MAXIMUM_EVIDENCE_ITEMS:
        raise ValueError("evidence artifacts exceed collection bounds")
    return tuple(EvidenceArtifact.from_dict(item) for item in values)


@dataclass(frozen=True, slots=True)
class EvidenceRunContext:
    """Bindings to compare with an independently supplied verification request."""

    invocation_id: str
    repository: str
    revision: str
    workflow: ContentIdentity
    target: ContentIdentity
    started_at: int
    finished_at: int

    def __post_init__(self) -> None:
        _name(self.invocation_id)
        repository = string_value(
            self.repository, "evidence.repository", max_length=1024
        )
        repository.encode("utf-8")
        if repository.strip() != repository or any(ord(c) < 32 for c in repository):
            raise ValueError(
                "repository identifier must not contain control or edge whitespace"
            )
        if not isinstance(self.revision, str) or not _REVISION.fullmatch(self.revision):
            raise ValueError(
                "evidence revision must be an exact lowercase Git object ID"
            )
        if not isinstance(self.workflow, ContentIdentity) or not isinstance(
            self.target, ContentIdentity
        ):
            raise ValueError("workflow and target must have immutable identities")
        int_value(self.started_at, "evidence.started_at")
        int_value(self.finished_at, "evidence.finished_at", minimum=self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "repository": self.repository,
            "revision": self.revision,
            "workflow": self.workflow.to_dict(),
            "target": self.target.to_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceRunContext:
        data = fields(
            value,
            path="EvidenceRunContext",
            required=frozenset(
                {
                    "invocation_id",
                    "repository",
                    "revision",
                    "workflow",
                    "target",
                    "started_at",
                    "finished_at",
                }
            ),
        )
        return cls(
            data["invocation_id"],
            data["repository"],
            data["revision"],
            ContentIdentity.from_dict(data["workflow"]),
            ContentIdentity.from_dict(data["target"]),
            data["started_at"],
            data["finished_at"],
        )


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    name: str
    blob: BlobRef

    def __post_init__(self) -> None:
        _name(self.name)
        _blob(self.blob)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "blob": self.blob.to_dict()}

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceArtifact:
        data = fields(
            value, path="EvidenceArtifact", required=frozenset({"name", "blob"})
        )
        return cls(data["name"], BlobRef.from_dict(data["blob"]))


@dataclass(frozen=True, slots=True)
class DerivationRun:
    context: EvidenceRunContext
    subject: BlobRef
    inputs: tuple[EvidenceArtifact, ...]
    journal: BlobRef
    status: str

    SCHEMA: ClassVar[str] = f"{_PREFIX}evidence-derivation-run"

    def __post_init__(self) -> None:
        _context(self.context)
        _blob(self.subject)
        _items(self.inputs)
        _blob(self.journal)
        _status(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "context": self.context.to_dict(),
            "subject": self.subject.to_dict(),
            "inputs": [item.to_dict() for item in self.inputs],
            "journal": self.journal.to_dict(),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Any) -> DerivationRun:
        data = contract_fields(
            value,
            path="DerivationRun",
            schema_uri=cls.SCHEMA,
            required=frozenset({"context", "subject", "inputs", "journal", "status"}),
        )
        return cls(
            EvidenceRunContext.from_dict(data["context"]),
            BlobRef.from_dict(data["subject"]),
            _parse_items(data["inputs"]),
            BlobRef.from_dict(data["journal"]),
            data["status"],
        )


@dataclass(frozen=True, slots=True)
class PlatformRun:
    context: EvidenceRunContext
    subject: BlobRef
    derivation: BlobRef
    environment: BlobRef
    checks: tuple[EvidenceArtifact, ...]
    status: str

    SCHEMA: ClassVar[str] = f"{_PREFIX}evidence-platform-run"

    def __post_init__(self) -> None:
        _context(self.context)
        _blob(self.subject)
        _blob(self.derivation, envelope=True)
        _blob(self.environment)
        _items(self.checks)
        _status(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "context": self.context.to_dict(),
            "subject": self.subject.to_dict(),
            "derivation": self.derivation.to_dict(),
            "environment": self.environment.to_dict(),
            "checks": [item.to_dict() for item in self.checks],
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Any) -> PlatformRun:
        data = contract_fields(
            value,
            path="PlatformRun",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"context", "subject", "derivation", "environment", "checks", "status"}
            ),
        )
        return cls(
            EvidenceRunContext.from_dict(data["context"]),
            BlobRef.from_dict(data["subject"]),
            BlobRef.from_dict(data["derivation"]),
            BlobRef.from_dict(data["environment"]),
            _parse_items(data["checks"]),
            data["status"],
        )


@dataclass(frozen=True, slots=True)
class EvidenceMatrix:
    """Declared coverage, requiring comparison with independently required cells."""

    context: EvidenceRunContext
    subject: BlobRef
    required_cells: tuple[str, ...]
    cells: tuple[EvidenceArtifact, ...]

    SCHEMA: ClassVar[str] = f"{_PREFIX}evidence-matrix"

    def __post_init__(self) -> None:
        _context(self.context)
        _blob(self.subject)
        _items(self.cells, envelope=True)
        if (
            not isinstance(self.required_cells, tuple)
            or not 1 <= len(self.required_cells) <= MAXIMUM_EVIDENCE_ITEMS
        ):
            raise ValueError("required cells must be a bounded nonempty tuple")
        for name in self.required_cells:
            _name(name)
        if self.required_cells != tuple(item.name for item in self.cells):
            raise ValueError(
                "matrix must cover every required cell exactly once in sorted order"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "context": self.context.to_dict(),
            "subject": self.subject.to_dict(),
            "required_cells": list(self.required_cells),
            "cells": [item.to_dict() for item in self.cells],
        }

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceMatrix:
        data = contract_fields(
            value,
            path="EvidenceMatrix",
            schema_uri=cls.SCHEMA,
            required=frozenset({"context", "subject", "required_cells", "cells"}),
        )
        required = list_value(data["required_cells"], "EvidenceMatrix.required_cells")
        if len(required) > MAXIMUM_EVIDENCE_ITEMS:
            raise ValueError("required cells exceed collection bounds")
        return cls(
            EvidenceRunContext.from_dict(data["context"]),
            BlobRef.from_dict(data["subject"]),
            tuple(required),
            _parse_items(data["cells"]),
        )


@dataclass(frozen=True, slots=True)
class EvidenceLocator:
    """A storage hint for one immutable object; never an ambient URL or file path."""

    store_id: str
    subject: BlobRef
    retained_until: int

    SCHEMA: ClassVar[str] = f"{_PREFIX}evidence-locator"

    def __post_init__(self) -> None:
        _name(self.store_id)
        _blob(self.subject)
        int_value(self.retained_until, "EvidenceLocator.retained_until")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "store_id": self.store_id,
            "subject": self.subject.to_dict(),
            "retained_until": self.retained_until,
        }

    @classmethod
    def from_dict(cls, value: Any) -> EvidenceLocator:
        data = contract_fields(
            value,
            path="EvidenceLocator",
            schema_uri=cls.SCHEMA,
            required=frozenset({"store_id", "subject", "retained_until"}),
        )
        return cls(
            data["store_id"], BlobRef.from_dict(data["subject"]), data["retained_until"]
        )


EvidencePredicate = DerivationRun | PlatformRun | EvidenceMatrix | EvidenceLocator
PREDICATE_TYPES = MappingProxyType(
    {
        kind.SCHEMA: kind
        for kind in (DerivationRun, PlatformRun, EvidenceMatrix, EvidenceLocator)
    }
)
