"""Durable adapter for intelligence engines that return structured objects."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from literate_ai.contracts import canonical_identity
from literate_ai.intelligence import (
    EvidenceItem,
    EvidenceLocation,
    IndexBinding,
    IntelligenceQuery,
    IntelligenceValidationError,
    StoredIndex,
    StoredIntelligenceResult,
)
from literate_ai.sources import TrustedSource
from literate_ai.storage import FileSystemCAS


class IntelligenceAdapterError(RuntimeError):
    """An intelligence engine violated its structured exact-binding contract."""


class StructuredIntelligenceEngine(Protocol):
    provider_id: str
    provider_version: str

    def ensure_index(
        self,
        source_path: Path,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def query(
        self,
        engine_index_key: str,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class DurableIntelligenceAdapter:
    """Normalize and persist engine JSON without scraping display output."""

    def __init__(
        self, engine: StructuredIntelligenceEngine, cas: FileSystemCAS
    ) -> None:
        if not engine.provider_id.strip() or not engine.provider_version.strip():
            raise ValueError(
                "intelligence engine requires provider identity and version"
            )
        self.engine = engine
        self.cas = cas

    def ensure_index(
        self,
        source: TrustedSource,
        configuration: Mapping[str, Any],
    ) -> StoredIndex:
        source.verify()
        configuration_value = dict(configuration)
        configuration_id = canonical_identity(
            {
                "provider_id": self.engine.provider_id,
                "provider_version": self.engine.provider_version,
                "configuration": configuration_value,
            }
        ).uri
        request = {
            "schema": "urn:literate-ai:schema:v1:intelligence-index-request",
            "source_snapshot_id": source.source_snapshot_id,
            "source_tree_id": source.source_tree_id,
            "source_snapshot_material": source.capture.snapshot.to_dict(),
            "source_tree_material": {
                "schema": "urn:literate-ai:schema:v1:source-tree-manifest",
                "entries": [
                    entry.to_dict() for entry in source.capture.snapshot.entries
                ],
                "aggregate_members": [
                    member.to_dict()
                    for member in source.capture.snapshot.aggregate_members
                ],
            },
            "source_files": [
                {
                    "path": entry.path,
                    "size": entry.size,
                    "identity": entry.identity.uri,
                }
                for entry in source.capture.snapshot.entries
                if entry.entry_type.value == "file"
            ],
            "configuration_id": configuration_id,
            "configuration": configuration_value,
        }
        raw = self._object(self.engine.ensure_index(source.tree_path, request), "index")
        self._exact(raw, "source_snapshot_id", source.source_snapshot_id)
        self._exact(raw, "source_tree_id", source.source_tree_id)
        engine_index_key = self._string(raw, "index_key")
        document_count = self._integer(raw, "document_count", minimum=0)
        raw_ref = self.cas.put_manifest(
            raw,
            media_type="application/vnd.literate-ai.intelligence-index-result+json",
        )
        binding = IndexBinding.create(
            source_snapshot_id=source.source_snapshot_id,
            source_tree_id=source.source_tree_id,
            provider_id=self.engine.provider_id,
            provider_version=self.engine.provider_version,
            configuration_id=configuration_id,
            engine_index_key=engine_index_key,
            artifact_ref=raw_ref,
            document_count=document_count,
        )
        binding_ref = self.cas.put_manifest(
            binding.to_dict(),
            media_type="application/vnd.literate-ai.intelligence-index-binding+json",
        )
        return StoredIndex(binding, binding_ref)

    def query(
        self,
        index: StoredIndex,
        query: IntelligenceQuery,
    ) -> StoredIntelligenceResult:
        self.cas.verify(index.binding_ref)
        if self.cas.get_manifest(index.binding_ref) != index.binding.to_dict():
            raise IntelligenceAdapterError("stored index binding is inconsistent")
        if (
            query.source_snapshot_id != index.binding.source_snapshot_id
            or query.index_id != index.binding.index_id
        ):
            raise IntelligenceAdapterError(
                "query does not bind the supplied exact index"
            )
        raw = self._object(
            self.engine.query(index.binding.engine_index_key, query.to_dict()),
            "query",
        )
        self._exact(raw, "source_snapshot_id", query.source_snapshot_id)
        self._exact(raw, "index_id", query.index_id)
        self._exact(raw, "query_id", query.query_id)
        raw_evidence = raw.get("evidence")
        if isinstance(raw_evidence, (str, bytes)) or not isinstance(
            raw_evidence, Sequence
        ):
            raise IntelligenceAdapterError("query result evidence must be an array")
        if len(raw_evidence) > query.max_results:
            raise IntelligenceAdapterError("query result exceeds the requested limit")
        evidence = tuple(
            self._evidence(query, item, index_number)
            for index_number, item in enumerate(raw_evidence)
        )
        if query.required and not evidence:
            raise IntelligenceAdapterError(
                "required intelligence query returned no evidence"
            )
        evidence_refs = tuple(
            self.cas.put_manifest(
                item.to_dict(),
                media_type="application/vnd.literate-ai.intelligence-evidence+json",
            )
            for item in evidence
        )
        raw_ref = self.cas.put_manifest(
            raw,
            media_type="application/vnd.literate-ai.intelligence-query-result+json",
        )
        normalized = {
            "schema": "urn:literate-ai:schema:v1:intelligence-result-manifest",
            "query": query.to_dict(),
            "evidence": [item.to_dict() for item in evidence],
            "evidence_refs": [item.to_dict() for item in evidence_refs],
            "raw_result_ref": raw_ref.to_dict(),
        }
        normalized_ref = self.cas.put_manifest(
            normalized,
            media_type="application/vnd.literate-ai.intelligence-result-manifest+json",
        )
        return StoredIntelligenceResult(
            query=query,
            evidence=evidence,
            evidence_refs=evidence_refs,
            raw_result_ref=raw_ref,
            normalized_result_ref=normalized_ref,
        )

    def _evidence(
        self,
        query: IntelligenceQuery,
        raw: object,
        index: int,
    ) -> EvidenceItem:
        value = self._object(raw, f"evidence[{index}]")
        raw_locations = value.get("locations")
        if isinstance(raw_locations, (str, bytes)) or not isinstance(
            raw_locations, Sequence
        ):
            raise IntelligenceAdapterError(
                f"evidence[{index}].locations must be an array"
            )
        locations = tuple(
            self._location(item, index, location_index)
            for location_index, item in enumerate(raw_locations)
        )
        raw_call_path = value.get("call_path", [])
        if isinstance(raw_call_path, (str, bytes)) or not isinstance(
            raw_call_path, Sequence
        ):
            raise IntelligenceAdapterError(
                f"evidence[{index}].call_path must be an array"
            )
        call_path = tuple(
            self._plain_string(item, f"evidence[{index}].call_path[{item_index}]")
            for item_index, item in enumerate(raw_call_path)
        )
        try:
            return EvidenceItem.create(
                query,
                kind=self._string(value, "kind"),
                summary=self._string(value, "summary"),
                content=self._string(value, "content"),
                locations=locations,
                call_path=call_path,
            )
        except IntelligenceValidationError as exc:
            raise IntelligenceAdapterError(
                f"evidence[{index}] is invalid: {exc}"
            ) from exc

    def _location(
        self, raw: object, evidence_index: int, location_index: int
    ) -> EvidenceLocation:
        value = self._object(
            raw,
            f"evidence[{evidence_index}].locations[{location_index}]",
        )
        try:
            return EvidenceLocation(
                path=self._string(value, "path"),
                start_line=self._integer(value, "start_line", minimum=1),
                end_line=self._integer(value, "end_line", minimum=1),
                symbol=self._optional_string(value, "symbol"),
            )
        except IntelligenceValidationError as exc:
            raise IntelligenceAdapterError("evidence location is invalid") from exc

    @staticmethod
    def _object(value: object, label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise IntelligenceAdapterError(f"{label} result must be a JSON object")
        if any(not isinstance(key, str) for key in value):
            raise IntelligenceAdapterError(f"{label} result keys must be strings")
        return dict(value)

    @classmethod
    def _string(cls, value: Mapping[str, Any], field: str) -> str:
        if field not in value:
            raise IntelligenceAdapterError(f"structured result is missing {field!r}")
        return cls._plain_string(value[field], field)

    @staticmethod
    def _plain_string(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise IntelligenceAdapterError(f"{field} must be a non-empty string")
        return value

    @staticmethod
    def _optional_string(value: Mapping[str, Any], field: str) -> str:
        raw = value.get(field, "")
        if not isinstance(raw, str):
            raise IntelligenceAdapterError(f"{field} must be a string")
        return raw

    @staticmethod
    def _integer(value: Mapping[str, Any], field: str, *, minimum: int) -> int:
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
            raise IntelligenceAdapterError(
                f"{field} must be an integer no smaller than {minimum}"
            )
        return raw

    @classmethod
    def _exact(cls, value: Mapping[str, Any], field: str, expected: str) -> None:
        if cls._string(value, field) != expected:
            raise IntelligenceAdapterError(
                f"structured result {field} does not match the exact request"
            )


__all__ = [
    "DurableIntelligenceAdapter",
    "IntelligenceAdapterError",
    "StructuredIntelligenceEngine",
]
