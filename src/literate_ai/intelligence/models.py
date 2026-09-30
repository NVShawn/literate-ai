"""Exact-bound structured intelligence query and evidence models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from literate_ai.contracts import canonical_identity
from literate_ai.storage import BlobRef, canonical_json_bytes


class IntelligenceValidationError(ValueError):
    """Structured intelligence data is malformed or bound to other source bytes."""


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntelligenceValidationError(f"{field_name} must not be empty")
    return value.strip()


def _relative_path(value: str, field_name: str) -> str:
    value = _text(value, field_name)
    if value.startswith("/") or "\\" in value:
        raise IntelligenceValidationError(f"{field_name} must be relative POSIX")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise IntelligenceValidationError(f"{field_name} must be normalized")
    return value


@dataclass(frozen=True, slots=True)
class IndexBinding:
    """A durable index artifact that is valid for one exact source snapshot."""

    index_id: str
    source_snapshot_id: str
    source_tree_id: str
    provider_id: str
    provider_version: str
    configuration_id: str
    engine_index_key: str
    artifact_ref: BlobRef
    document_count: int

    def __post_init__(self) -> None:
        for name in (
            "index_id",
            "source_snapshot_id",
            "source_tree_id",
            "provider_id",
            "provider_version",
            "configuration_id",
            "engine_index_key",
        ):
            _text(getattr(self, name), name)
        if self.document_count < 0:
            raise IntelligenceValidationError("document_count must not be negative")
        if self.index_id != canonical_identity(self.identity_material()).uri:
            raise IntelligenceValidationError(
                "index_id does not match its exact binding"
            )

    @classmethod
    def create(
        cls,
        *,
        source_snapshot_id: str,
        source_tree_id: str,
        provider_id: str,
        provider_version: str,
        configuration_id: str,
        engine_index_key: str,
        artifact_ref: BlobRef,
        document_count: int,
    ) -> IndexBinding:
        values = {
            "source_snapshot_id": source_snapshot_id,
            "source_tree_id": source_tree_id,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "configuration_id": configuration_id,
            "engine_index_key": engine_index_key,
            "artifact_ref": artifact_ref.to_dict(),
            "document_count": document_count,
        }
        return cls(
            index_id=canonical_identity(values).uri,
            source_snapshot_id=source_snapshot_id,
            source_tree_id=source_tree_id,
            provider_id=provider_id,
            provider_version=provider_version,
            configuration_id=configuration_id,
            engine_index_key=engine_index_key,
            artifact_ref=artifact_ref,
            document_count=document_count,
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "source_snapshot_id": self.source_snapshot_id,
            "source_tree_id": self.source_tree_id,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "configuration_id": self.configuration_id,
            "engine_index_key": self.engine_index_key,
            "artifact_ref": self.artifact_ref.to_dict(),
            "document_count": self.document_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v1:intelligence-index-binding",
            "index_id": self.index_id,
            **self.identity_material(),
        }


@dataclass(frozen=True, slots=True)
class IntelligenceQuery:
    """One structured question against one exact index."""

    query_id: str
    source_snapshot_id: str
    index_id: str
    text: str
    purpose: str
    required: bool = False
    selected: bool = True
    max_results: int = 20

    def __post_init__(self) -> None:
        for name in ("query_id", "source_snapshot_id", "index_id", "text", "purpose"):
            _text(getattr(self, name), name)
        if self.max_results <= 0:
            raise IntelligenceValidationError("max_results must be positive")
        if self.query_id != canonical_identity(self.identity_material()).uri:
            raise IntelligenceValidationError("query_id does not match its request")

    @classmethod
    def create(
        cls,
        index: IndexBinding,
        *,
        text: str,
        purpose: str,
        required: bool = False,
        selected: bool = True,
        max_results: int = 20,
    ) -> IntelligenceQuery:
        values = {
            "source_snapshot_id": index.source_snapshot_id,
            "index_id": index.index_id,
            "text": text,
            "purpose": purpose,
            "required": required,
            "selected": selected,
            "max_results": max_results,
        }
        return cls(query_id=canonical_identity(values).uri, **values)

    def identity_material(self) -> dict[str, Any]:
        return {
            "source_snapshot_id": self.source_snapshot_id,
            "index_id": self.index_id,
            "text": self.text,
            "purpose": self.purpose,
            "required": self.required,
            "selected": self.selected,
            "max_results": self.max_results,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v1:intelligence-query",
            "query_id": self.query_id,
            **self.identity_material(),
        }


@dataclass(frozen=True, slots=True)
class StoredIndex:
    """A normalized exact index binding stored as immutable JSON."""

    binding: IndexBinding
    binding_ref: BlobRef

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v1:stored-intelligence-index",
            "binding": self.binding.to_dict(),
            "binding_ref": self.binding_ref.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceLocation:
    path: str
    start_line: int
    end_line: int
    symbol: str = ""

    def __post_init__(self) -> None:
        _relative_path(self.path, "EvidenceLocation.path")
        if self.start_line <= 0 or self.end_line < self.start_line:
            raise IntelligenceValidationError("evidence line range is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """Verbatim structured evidence with complete source/index/query binding."""

    evidence_id: str
    source_snapshot_id: str
    index_id: str
    query_id: str
    kind: str
    summary: str
    content: str
    locations: tuple[EvidenceLocation, ...]
    call_path: tuple[str, ...] = ()
    required: bool = False
    selected: bool = True

    def __post_init__(self) -> None:
        for name in (
            "evidence_id",
            "source_snapshot_id",
            "index_id",
            "query_id",
            "kind",
            "summary",
            "content",
        ):
            _text(getattr(self, name), name)
        if not self.locations:
            raise IntelligenceValidationError("evidence requires a source location")
        if self.evidence_id != canonical_identity(self.identity_material()).uri:
            raise IntelligenceValidationError("evidence_id does not match its content")

    @classmethod
    def create(
        cls,
        query: IntelligenceQuery,
        *,
        kind: str,
        summary: str,
        content: str,
        locations: tuple[EvidenceLocation, ...],
        call_path: tuple[str, ...] = (),
    ) -> EvidenceItem:
        values = {
            "source_snapshot_id": query.source_snapshot_id,
            "index_id": query.index_id,
            "query_id": query.query_id,
            "kind": kind,
            "summary": summary,
            "content": content,
            "locations": [item.to_dict() for item in locations],
            "call_path": list(call_path),
            "required": query.required,
            "selected": query.selected,
        }
        return cls(
            evidence_id=canonical_identity(values).uri,
            source_snapshot_id=query.source_snapshot_id,
            index_id=query.index_id,
            query_id=query.query_id,
            kind=kind,
            summary=summary,
            content=content,
            locations=locations,
            call_path=call_path,
            required=query.required,
            selected=query.selected,
        )

    def identity_material(self) -> dict[str, Any]:
        return {
            "source_snapshot_id": self.source_snapshot_id,
            "index_id": self.index_id,
            "query_id": self.query_id,
            "kind": self.kind,
            "summary": self.summary,
            "content": self.content,
            "locations": [item.to_dict() for item in self.locations],
            "call_path": list(self.call_path),
            "required": self.required,
            "selected": self.selected,
        }

    @property
    def encoded_size(self) -> int:
        return len(canonical_json_bytes(self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v1:intelligence-evidence",
            "evidence_id": self.evidence_id,
            **self.identity_material(),
        }


@dataclass(frozen=True, slots=True)
class StoredIntelligenceResult:
    query: IntelligenceQuery
    evidence: tuple[EvidenceItem, ...]
    evidence_refs: tuple[BlobRef, ...]
    raw_result_ref: BlobRef
    normalized_result_ref: BlobRef

    def __post_init__(self) -> None:
        if len(self.evidence) != len(self.evidence_refs):
            raise IntelligenceValidationError(
                "every evidence item requires a durable ref"
            )
        for item in self.evidence:
            if (
                item.source_snapshot_id != self.query.source_snapshot_id
                or item.index_id != self.query.index_id
                or item.query_id != self.query.query_id
            ):
                raise IntelligenceValidationError(
                    "evidence binding does not match query"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v1:stored-intelligence-result",
            "query": self.query.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "raw_result_ref": self.raw_result_ref.to_dict(),
            "normalized_result_ref": self.normalized_result_ref.to_dict(),
        }


__all__ = [
    "EvidenceItem",
    "EvidenceLocation",
    "IndexBinding",
    "IntelligenceQuery",
    "IntelligenceValidationError",
    "StoredIntelligenceResult",
    "StoredIndex",
]
