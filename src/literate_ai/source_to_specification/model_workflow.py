"""Code-intelligence and model-backed inverse specification orchestration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.executable_components._common import portable_name
from literate_ai.contracts.library_imports import AuthoredLibraryImport
from literate_ai.diagnostics import log_operation

from .behavioral_surfaces import (
    BehavioralInterfaceKind,
    BehavioralSurfaceInventory,
    BehavioralSurfaceInventoryItem,
    collect_behavioral_surface_inventory,
    create_model_surface_proposal,
    observation_facet_maps_interface,
    reconcile_behavioral_surface_inventory,
)
from .contracts import (
    BehaviorObservation,
    BehaviorSurface,
    ClaimKind,
    ComponentCapabilityContractDraft,
    ComponentDefinitionDraft,
    ComponentEntrypointDraft,
    ComponentGraphDraft,
    ComponentGraphEdgeDraft,
    ComponentGraphNodeDraft,
    CoverageState,
    DraftArtifact,
    DraftScenario,
    DraftStatement,
    EvidenceReference,
    RunMode,
    SkillRef,
    SourceToSpecificationRequest,
    SourceToSpecificationResult,
    SpecAuthoringSkill,
    SpecAuthoringSkillSet,
    canonical_digest,
    canonical_value,
)
from .errors import SourceToSpecificationError
from .evidence_partitions import (
    EvidencePartitionManifest,
    ModelEvidenceBatchPlan,
    build_evidence_partition_manifest,
    plan_model_evidence_batches,
)
from .inventory import SourceInventory, source_inventory_from_dict
from .literal_data import (
    canonical_literal_table_json,
    literal_asset_path,
    python_literal_tables,
)
from .model_ports import InverseLanguageTranslator, SourceIntelligenceCollector
from .skills import builtin_skill_set, resolve_skill_set
from .synthesis import (
    benefits_from_literate_markdown,
    capability_contract_path,
    component_name,
    component_specification_path,
    render_capability_contract_markdown,
    render_component_openspec_markdown,
    render_literate_markdown_artifacts,
    validate_literate_markdown_draft,
    validate_openspec_draft,
)
from .workflow import SourceToSpecificationWorkflow, SourceTreeFingerprint

MODEL_TRANSLATION_OUTPUT_SCHEMA = (
    "urn:literate-ai:schema:v2:source-to-specification-model-output"
)
LEGACY_MODEL_CALL_JOURNAL_SCHEMA = (
    "urn:literate-ai:schema:v1:source-to-specification-model-call-journal"
)
PREVIOUS_MODEL_CALL_JOURNAL_SCHEMA = (
    "urn:literate-ai:schema:v2:source-to-specification-model-call-journal"
)
MODEL_CALL_JOURNAL_SCHEMA = (
    "urn:literate-ai:schema:v3:source-to-specification-model-call-journal"
)
LEGACY_SOURCE_INTELLIGENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:source-to-specification-intelligence"
)
SOURCE_INTELLIGENCE_SCHEMA = (
    "urn:literate-ai:schema:v3:source-to-specification-intelligence"
)
LEGACY_SOURCE_TRANSLATION_RUN_SCHEMA = (
    "urn:literate-ai:schema:v2:source-to-specification-translation-run"
)
PREVIOUS_SOURCE_TRANSLATION_RUN_SCHEMA = (
    "urn:literate-ai:schema:v4:source-to-specification-translation-run"
)
LEGACY_SEMANTIC_SOURCE_TRANSLATION_RUN_SCHEMA = (
    "urn:literate-ai:schema:v3:source-to-specification-translation-run"
)
SOURCE_TRANSLATION_RUN_SCHEMA = (
    "urn:literate-ai:schema:v5:source-to-specification-translation-run"
)
MODEL_TRANSLATION_MODE = "source-intelligence-coding-cli"
LEGACY_MODEL_TRANSLATION_MODE = "codegraph-coding-cli"
INVERSE_EVIDENCE_CUSTODY_SCHEMA = "urn:literate-ai:schema:v1:inverse-evidence-custody"

SUPPORTED_INVERSE_LANGUAGES = ("cpp", "javascript", "python", "rust")
LANGUAGE_SKILL_IDS = {
    "cpp": "language-cpp",
    "javascript": "language-javascript",
    "python": "language-python",
    "rust": "language-rust",
    "typescript": "language-javascript",
}
_SHA256_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")


def _portable_graph_identifier(value: object, path: str) -> str:
    """Admit an exact graph key that can cross the authored-Component boundary."""

    raw = _text(value, path)
    try:
        return portable_name(raw, path)
    except ContractValidationError as error:
        raise SourceToSpecificationError(
            "model_translation.component_graph_identifier_invalid",
            f"{path} must match ^[a-z0-9][a-z0-9._-]{{0,126}}$; "
            "model Component graph identifiers are never normalized",
        ) from error


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceToSpecificationError(
            "model_translation.invalid_text", f"{field} must be a non-empty string"
        )
    return value.strip()


def _component_title(coordinate: str) -> str:
    """Derive non-semantic display text so language calls cannot conflict on it."""

    segment = coordinate.rsplit("/", 1)[-1]
    title = re.sub(r"[-_]+", " ", segment).strip().title()
    return title or coordinate


def _sha256_identity(value: object, field: str) -> str:
    normalized = _text(value, field)
    if _SHA256_IDENTITY.fullmatch(normalized) is None:
        raise SourceToSpecificationError(
            "model_translation.identity_invalid",
            f"{field} must be an exact sha256 content identity",
        )
    return normalized


def _string_tuple(
    value: object, field: str, *, nonempty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise SourceToSpecificationError(
            "model_translation.invalid_array", f"{field} must be an array of strings"
        )
    normalized = tuple(item.strip() for item in value)
    if len(normalized) != len(set(normalized)) or (nonempty and not normalized):
        raise SourceToSpecificationError(
            "model_translation.invalid_array",
            f"{field} must contain unique values"
            + (" and not be empty" if nonempty else ""),
        )
    return normalized


def _model_string_set(
    value: object, field: str, *, nonempty: bool = False
) -> tuple[str, ...]:
    """Canonicalize a model-emitted JSON set while retaining strict element types."""

    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise SourceToSpecificationError(
            "model_translation.invalid_array", f"{field} must be an array of strings"
        )
    normalized = tuple(sorted({item.strip() for item in value}))
    if nonempty and not normalized:
        raise SourceToSpecificationError(
            "model_translation.invalid_array", f"{field} must not be empty"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ModelEvidence:
    """Exact evidence content made available to one inverse model call."""

    reference: EvidenceReference
    language: str
    kind: str
    content: str
    start_line: int
    end_line: int
    call_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reference, EvidenceReference):
            raise TypeError("model evidence requires an EvidenceReference")
        object.__setattr__(self, "language", _text(self.language, "language"))
        object.__setattr__(self, "kind", _text(self.kind, "kind"))
        if not isinstance(self.content, str) or not self.content:
            raise SourceToSpecificationError(
                "model_translation.evidence_empty", "model evidence content is empty"
            )
        content_digest = (
            "sha256:" + hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        )
        if self.reference.content_digest != content_digest:
            raise SourceToSpecificationError(
                "model_translation.evidence_digest_mismatch",
                "model evidence digest does not match its exact content",
            )
        if (
            isinstance(self.start_line, bool)
            or not isinstance(self.start_line, int)
            or isinstance(self.end_line, bool)
            or not isinstance(self.end_line, int)
            or self.start_line <= 0
            or self.end_line < self.start_line
        ):
            raise SourceToSpecificationError(
                "model_translation.evidence_location_invalid",
                "model evidence line range is invalid",
            )
        if len(self.call_path) != len(set(self.call_path)) or any(
            not item.strip() for item in self.call_path
        ):
            raise SourceToSpecificationError(
                "model_translation.call_path_invalid",
                "model evidence call path must contain unique non-empty symbols",
            )

    def to_dict(self, *, include_content: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "reference": canonical_value(self.reference),
            "language": self.language,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "call_path": list(self.call_path),
        }
        if include_content:
            result["content"] = self.content
        return result


@dataclass(frozen=True, slots=True)
class IntelligenceQueryRecord:
    query_id: str
    language: str
    text: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("query_id", "language", "text"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise SourceToSpecificationError(
                "model_translation.query_evidence_duplicate",
                "query evidence IDs must be unique",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "query_id": self.query_id,
            "language": self.language,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
        }


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _source_path(value: object, field: str) -> str:
    normalized = _text(value, field)
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or "\\" in normalized
        or path.as_posix() != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SourceToSpecificationError(
            "model_translation.relationship_path_invalid",
            f"{field} must be a normalized relative POSIX path",
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ModelRelationshipEvidence:
    """One provider-derived relation; useful evidence, never source authority."""

    relationship_id: str
    kind: str
    source_symbol: str
    source_path: str
    source_language: str
    target_symbol: str
    target_path: str
    target_language: str
    line: int | None = None
    column: int | None = None
    provenance: str | None = None
    resolution_method: str | None = None
    confidence_basis_points: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "kind",
            "source_symbol",
            "source_language",
            "target_symbol",
            "target_language",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self, "source_path", _source_path(self.source_path, "source_path")
        )
        object.__setattr__(
            self, "target_path", _source_path(self.target_path, "target_path")
        )
        for field in ("line", "column"):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise SourceToSpecificationError(
                    "model_translation.relationship_location_invalid",
                    "relationship line and column must be non-negative integers",
                )
        if self.line == 0:
            raise SourceToSpecificationError(
                "model_translation.relationship_location_invalid",
                "relationship line must be positive when present",
            )
        for field in ("provenance", "resolution_method"):
            object.__setattr__(self, field, _optional_text(getattr(self, field), field))
        confidence = self.confidence_basis_points
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or not 0 <= confidence <= 10_000
        ):
            raise SourceToSpecificationError(
                "model_translation.relationship_confidence_invalid",
                "provider relationship confidence must be integer basis points",
            )
        _sha256_identity(self.relationship_id, "relationship_id")
        if self.relationship_id != canonical_digest(self.identity_material()):
            raise SourceToSpecificationError(
                "model_translation.relationship_identity_mismatch",
                "relationship identity does not bind its complete evidence",
            )

    @classmethod
    def create(cls, **values: object) -> ModelRelationshipEvidence:
        material = dict(values)
        return cls(
            relationship_id=canonical_digest(material),
            **material,  # type: ignore[arg-type]
        )

    def identity_material(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source_symbol": self.source_symbol,
            "source_path": self.source_path,
            "source_language": self.source_language,
            "target_symbol": self.target_symbol,
            "target_path": self.target_path,
            "target_language": self.target_language,
            "line": self.line,
            "column": self.column,
            "provenance": self.provenance,
            "resolution_method": self.resolution_method,
            "confidence_basis_points": self.confidence_basis_points,
        }

    def to_dict(self) -> dict[str, object]:
        return {"relationship_id": self.relationship_id, **self.identity_material()}


@dataclass(frozen=True, slots=True)
class UnresolvedRelationshipEvidence:
    """One bounded unresolved provider reference retained as uncertainty evidence."""

    unresolved_id: str
    source_symbol: str
    source_path: str
    source_language: str
    reference_name: str
    reference_kind: str
    line: int
    column: int
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "source_symbol",
            "source_language",
            "reference_name",
            "reference_kind",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self, "source_path", _source_path(self.source_path, "source_path")
        )
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (self.line, self.column)
            )
            or self.line == 0
        ):
            raise SourceToSpecificationError(
                "model_translation.unresolved_location_invalid",
                "unresolved relationship location is invalid",
            )
        if len(self.candidates) != len(set(self.candidates)) or any(
            not isinstance(item, str) or not item.strip() for item in self.candidates
        ):
            raise SourceToSpecificationError(
                "model_translation.unresolved_candidates_invalid",
                "unresolved relationship candidates must be unique strings",
            )
        _sha256_identity(self.unresolved_id, "unresolved_id")
        if self.unresolved_id != canonical_digest(self.identity_material()):
            raise SourceToSpecificationError(
                "model_translation.unresolved_identity_mismatch",
                "unresolved relationship identity does not bind its evidence",
            )

    @classmethod
    def create(cls, **values: object) -> UnresolvedRelationshipEvidence:
        material = dict(values)
        return cls(
            unresolved_id=canonical_digest(material),
            **material,  # type: ignore[arg-type]
        )

    def identity_material(self) -> dict[str, object]:
        return {
            "source_symbol": self.source_symbol,
            "source_path": self.source_path,
            "source_language": self.source_language,
            "reference_name": self.reference_name,
            "reference_kind": self.reference_kind,
            "line": self.line,
            "column": self.column,
            "candidates": list(self.candidates),
        }

    def to_dict(self) -> dict[str, object]:
        return {"unresolved_id": self.unresolved_id, **self.identity_material()}


@dataclass(frozen=True, slots=True)
class SourceIntelligence:
    """One exact provider artifact plus bounded, explicitly derived evidence."""

    authority_source_snapshot_id: str
    indexed_source_snapshot_id: str
    indexed_source_tree_id: str
    indexed_source_files: tuple[tuple[str, int, str], ...]
    source_content_identity: str
    source_material_identity: str
    intelligence_evidence_identity: str
    provider_id: str
    provider_version: str
    runtime_version: str
    executable_identity: str
    artifact_identity: str
    artifact_media_type: str
    capabilities: tuple[str, ...]
    provider_properties: tuple[str, ...]
    document_count: int
    symbol_count: int
    relationship_count: int
    unresolved_relationship_count: int
    warning_count: int
    languages: tuple[str, ...]
    evidence: tuple[ModelEvidence, ...]
    relationships: tuple[ModelRelationshipEvidence, ...]
    unresolved_relationships: tuple[UnresolvedRelationshipEvidence, ...]
    queries: tuple[IntelligenceQueryRecord, ...]
    record_identity: str | None = None

    SCHEMA = SOURCE_INTELLIGENCE_SCHEMA

    def __post_init__(self) -> None:
        for field in (
            "provider_id",
            "provider_version",
            "runtime_version",
            "artifact_media_type",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        for field in (
            "authority_source_snapshot_id",
            "indexed_source_snapshot_id",
            "indexed_source_tree_id",
            "source_content_identity",
            "source_material_identity",
            "intelligence_evidence_identity",
            "executable_identity",
            "artifact_identity",
        ):
            object.__setattr__(
                self, field, _sha256_identity(getattr(self, field), field)
            )
        if self.record_identity is not None:
            object.__setattr__(
                self,
                "record_identity",
                _sha256_identity(self.record_identity, "record_identity"),
            )
        if tuple(sorted(self.indexed_source_files)) != self.indexed_source_files or len(
            self.indexed_source_files
        ) != len({path for path, _size, _identity in self.indexed_source_files}):
            raise SourceToSpecificationError(
                "model_translation.index_manifest_invalid",
                "indexed source file evidence must be canonical and path-unique",
            )
        for path, size, identity in self.indexed_source_files:
            if (
                not isinstance(path, str)
                or not path
                or type(size) is not int
                or size < 0
            ):
                raise SourceToSpecificationError(
                    "model_translation.index_manifest_invalid",
                    "indexed source file evidence is malformed",
                )
            _sha256_identity(identity, "indexed source file identity")
        if self.source_content_identity != canonical_digest(
            [
                {"path": path, "size": size, "identity": identity}
                for path, size, identity in self.indexed_source_files
            ]
        ):
            raise SourceToSpecificationError(
                "model_translation.index_manifest_invalid",
                "indexed source file evidence has another content identity",
            )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.document_count,
                self.symbol_count,
                self.relationship_count,
                self.unresolved_relationship_count,
                self.warning_count,
            )
        ):
            raise SourceToSpecificationError(
                "model_translation.index_count_invalid",
                "source-intelligence counts must be non-negative integers",
            )
        for field in ("capabilities", "provider_properties"):
            values = getattr(self, field)
            if tuple(sorted(set(values))) != values or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise SourceToSpecificationError(
                    "model_translation.provider_metadata_invalid",
                    f"{field} must be unique canonical strings",
                )
        normalized_languages = tuple(sorted(set(self.languages)))
        if not normalized_languages or any(
            language not in SUPPORTED_INVERSE_LANGUAGES
            for language in normalized_languages
        ):
            raise SourceToSpecificationError(
                "model_translation.language_unsupported",
                "source intelligence requires supported inverse languages",
            )
        object.__setattr__(self, "languages", normalized_languages)
        evidence_ids = tuple(item.reference.evidence_id for item in self.evidence)
        if not evidence_ids or len(evidence_ids) != len(set(evidence_ids)):
            raise SourceToSpecificationError(
                "model_translation.evidence_invalid",
                "source intelligence requires unique evidence",
            )
        if any(
            item.reference.source_snapshot_id != self.authority_source_snapshot_id
            for item in self.evidence
        ):
            raise SourceToSpecificationError(
                "model_translation.evidence_snapshot_mismatch",
                "source intelligence evidence describes another source snapshot",
            )
        if any(item.language not in normalized_languages for item in self.evidence):
            raise SourceToSpecificationError(
                "model_translation.evidence_language_mismatch",
                "source intelligence evidence describes an unselected language",
            )
        indexed_paths = frozenset(
            path for path, _size, _identity in self.indexed_source_files
        )
        relationship_ids = tuple(item.relationship_id for item in self.relationships)
        unresolved_ids = tuple(
            item.unresolved_id for item in self.unresolved_relationships
        )
        if (
            len(relationship_ids) != len(set(relationship_ids))
            or len(unresolved_ids) != len(set(unresolved_ids))
            or any(
                item.source_path not in indexed_paths
                or item.target_path not in indexed_paths
                for item in self.relationships
            )
            or any(
                item.source_path not in indexed_paths
                for item in self.unresolved_relationships
            )
            or len(self.relationships) > self.relationship_count
            or len(self.unresolved_relationships) > self.unresolved_relationship_count
        ):
            raise SourceToSpecificationError(
                "model_translation.relationship_evidence_invalid",
                "bounded relationship evidence is inconsistent with its artifact",
            )
        query_ids = tuple(item.query_id for item in self.queries)
        known_evidence = frozenset(evidence_ids)
        if len(query_ids) != len(set(query_ids)) or any(
            item.language not in normalized_languages
            or not set(item.evidence_ids).issubset(known_evidence)
            for item in self.queries
        ):
            raise SourceToSpecificationError(
                "model_translation.query_invalid",
                "source intelligence queries must be unique and cite admitted evidence",
            )

    @property
    def identity(self) -> str:
        return self.record_identity or canonical_digest(
            self.to_dict(include_content=True)
        )

    @property
    def source_snapshot_id(self) -> str:
        """Compatibility spelling for the explicitly authoritative source identity."""

        return self.authority_source_snapshot_id

    def to_dict(self, *, include_content: bool = True) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "authority_source_snapshot_id": self.authority_source_snapshot_id,
            "indexed_source_snapshot_id": self.indexed_source_snapshot_id,
            "indexed_source_tree_id": self.indexed_source_tree_id,
            "indexed_source_files": [
                {"path": path, "size": size, "identity": identity}
                for path, size, identity in self.indexed_source_files
            ],
            "source_content_identity": self.source_content_identity,
            "source_material_identity": self.source_material_identity,
            "intelligence_evidence_identity": self.intelligence_evidence_identity,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "runtime_version": self.runtime_version,
            "executable_identity": self.executable_identity,
            "artifact_identity": self.artifact_identity,
            "artifact_media_type": self.artifact_media_type,
            "capabilities": list(self.capabilities),
            "provider_properties": list(self.provider_properties),
            "document_count": self.document_count,
            "symbol_count": self.symbol_count,
            "relationship_count": self.relationship_count,
            "unresolved_relationship_count": self.unresolved_relationship_count,
            "warning_count": self.warning_count,
            "languages": list(self.languages),
            "evidence": [
                item.to_dict(include_content=include_content) for item in self.evidence
            ],
            "relationships": [item.to_dict() for item in self.relationships],
            "unresolved_relationships": [
                item.to_dict() for item in self.unresolved_relationships
            ],
            "queries": [item.to_dict() for item in self.queries],
        }


@dataclass(frozen=True, slots=True)
class ModelObservationDraft:
    language: str
    skill_id: str
    facet: str
    claim_kind: ClaimKind
    requirement: str
    capability: str
    scenario: DraftScenario
    evidence_ids: tuple[str, ...]
    confidence_basis_points: int
    scope: str
    component_coordinate: str

    def __post_init__(self) -> None:
        for field in (
            "language",
            "skill_id",
            "facet",
            "requirement",
            "capability",
            "component_coordinate",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if not isinstance(self.claim_kind, ClaimKind):
            try:
                object.__setattr__(self, "claim_kind", ClaimKind(self.claim_kind))
            except ValueError as exc:
                raise SourceToSpecificationError(
                    "model_translation.claim_kind_invalid",
                    "model observation claim kind is unsupported",
                ) from exc
        if not isinstance(self.scenario, DraftScenario):
            raise TypeError("model observation scenario must be a DraftScenario")
        if len(self.evidence_ids) != len(set(self.evidence_ids)) or (
            not self.evidence_ids and self.claim_kind is not ClaimKind.UNKNOWN
        ):
            raise SourceToSpecificationError(
                "model_translation.observation_evidence_invalid",
                "model observations require unique exact evidence",
            )
        if (
            isinstance(self.confidence_basis_points, bool)
            or not isinstance(self.confidence_basis_points, int)
            or not 0 <= self.confidence_basis_points <= 10_000
        ):
            raise SourceToSpecificationError(
                "model_translation.confidence_invalid",
                "model observation confidence must be integer basis points from "
                "zero through 10000",
            )
        allowed_scopes = {"base", f"flavor:{self.language}"}
        if self.scope not in allowed_scopes:
            raise SourceToSpecificationError(
                "model_translation.scope_invalid",
                "model observation scope must be base or its exact language Flavor",
            )

    @property
    def observation_id(self) -> str:
        return "model-observation:" + canonical_digest(
            canonical_value(self)
        ).removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class ModelCallJournal:
    call_id: str
    language: str
    coding_cli: str
    executable: str
    executable_identity: str
    model: str | None
    command: tuple[str, ...]
    prompt: str
    response: Mapping[str, object]
    response_text: str
    stdout: str
    stderr: str
    request_identity: str
    response_identity: str
    command_identity: str
    selection_identity: str
    tool_binding_identity: str
    isolation: Mapping[str, object]
    environment_keys: tuple[str, ...]
    skill_refs: tuple[SkillRef, ...]
    intelligence_identity: str
    egress_policy_id: str
    evidence_ids: tuple[str, ...]
    partition_ordinal: int
    partition_count: int
    record_schema: str = MODEL_CALL_JOURNAL_SCHEMA

    def __post_init__(self) -> None:
        if self.record_schema not in {
            MODEL_CALL_JOURNAL_SCHEMA,
            PREVIOUS_MODEL_CALL_JOURNAL_SCHEMA,
            LEGACY_MODEL_CALL_JOURNAL_SCHEMA,
        }:
            raise SourceToSpecificationError(
                "model_translation.schema_unsupported",
                "model call journal uses an unsupported schema",
            )
        for field in (
            "language",
            "coding_cli",
            "executable",
            "egress_policy_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        for field in (
            "call_id",
            "executable_identity",
            "request_identity",
            "response_identity",
            "command_identity",
            "selection_identity",
            "tool_binding_identity",
            "intelligence_identity",
        ):
            object.__setattr__(
                self, field, _sha256_identity(getattr(self, field), field)
            )
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or not isinstance(self.response_text, str)
            or not self.response_text.strip()
        ):
            raise SourceToSpecificationError(
                "model_translation.journal_incomplete",
                "model call journal requires exact non-empty prompt and response text",
            )
        if self.model is not None:
            object.__setattr__(self, "model", _text(self.model, "model"))
        if not isinstance(self.isolation, Mapping) or any(
            not isinstance(key, str) for key in self.isolation
        ):
            raise SourceToSpecificationError(
                "model_translation.journal_incomplete",
                "model call journal isolation must be an object",
            )
        if (
            self.language not in SUPPORTED_INVERSE_LANGUAGES
            or self.coding_cli not in {"codex", "claude", "cursor-agent", "opencode"}
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
            or not self.skill_refs
            or len({item.skill_id for item in self.skill_refs}) != len(self.skill_refs)
            or len(set(self.environment_keys)) != len(self.environment_keys)
            or any(
                not isinstance(item, str) or not item for item in self.environment_keys
            )
            or not isinstance(self.response, Mapping)
            or any(not isinstance(key, str) for key in self.response)
            or (
                self.record_schema != LEGACY_MODEL_CALL_JOURNAL_SCHEMA
                and not self.evidence_ids
            )
            or self.evidence_ids != tuple(sorted(set(self.evidence_ids)))
            or any(not isinstance(item, str) or not item for item in self.evidence_ids)
            or isinstance(self.partition_ordinal, bool)
            or not isinstance(self.partition_ordinal, int)
            or self.partition_ordinal < 0
            or isinstance(self.partition_count, bool)
            or not isinstance(self.partition_count, int)
            or self.partition_count < 1
            or self.partition_ordinal >= self.partition_count
        ):
            raise SourceToSpecificationError(
                "model_translation.journal_incomplete",
                "model call journal requires command and exact skills",
            )
        if self.call_id != canonical_digest(self.identity_material()):
            raise SourceToSpecificationError(
                "model_translation.journal_identity_mismatch",
                "model call journal identity does not bind its complete transcript",
            )

    def identity_material(self) -> dict[str, object]:
        material = {
            "language": self.language,
            "coding_cli": self.coding_cli,
            "executable": self.executable,
            "executable_identity": self.executable_identity,
            "model": self.model,
            "command": list(self.command),
            "prompt": self.prompt,
            "response": canonical_value(self.response),
            "response_text": self.response_text,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "request_identity": self.request_identity,
            "response_identity": self.response_identity,
            "command_identity": self.command_identity,
            "selection_identity": self.selection_identity,
            "tool_binding_identity": self.tool_binding_identity,
            "isolation": canonical_value(self.isolation),
            "environment_keys": list(self.environment_keys),
            "skill_refs": canonical_value(self.skill_refs),
            "intelligence_identity": self.intelligence_identity,
            "egress_policy_id": self.egress_policy_id,
        }
        if self.record_schema != LEGACY_MODEL_CALL_JOURNAL_SCHEMA:
            material.update(
                {
                    "evidence_ids": list(self.evidence_ids),
                    "partition_ordinal": self.partition_ordinal,
                    "partition_count": self.partition_count,
                }
            )
        return material

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.record_schema,
            "call_id": self.call_id,
            **self.identity_material(),
        }


@dataclass(frozen=True, slots=True)
class ModelTranslation:
    language: str
    observations: tuple[ModelObservationDraft, ...]
    journal: ModelCallJournal
    component_graph: ComponentGraphDraft

    def __post_init__(self) -> None:
        if self.language not in SUPPORTED_INVERSE_LANGUAGES:
            raise SourceToSpecificationError(
                "model_translation.language_unsupported",
                "model translation language is unsupported",
            )
        if not self.observations or any(
            item.language != self.language for item in self.observations
        ):
            raise SourceToSpecificationError(
                "model_translation.output_empty",
                "each language translator must emit language-bound observations",
            )
        if self.journal.language != self.language:
            raise SourceToSpecificationError(
                "model_translation.journal_language_mismatch",
                "model journal describes another language",
            )
        if not isinstance(self.component_graph, ComponentGraphDraft):
            raise TypeError("model translation requires a ComponentGraphDraft")
        if not any(
            item.scope == "base"
            and item.claim_kind in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
            for item in self.observations
        ) or not any(
            item.scope == f"flavor:{self.language}" for item in self.observations
        ):
            raise SourceToSpecificationError(
                "model_translation.output_incomplete",
                "each language translator must emit observed base behavior and its "
                "language-Flavor evidence",
            )


# A single coding-CLI translation call classifying a genuinely observable, in-scope
# behavioral surface as only "inferred-intent" (rather than the stronger
# observed-current-behavior/compatibility-quirk claim kinds) has been observed to be
# a non-deterministic model output-quality issue, not a specification or environment
# problem: an identical `spec derive` re-run against the exact same, unchanged source
# has flipped that single surface from `unsupported` ("observations were not
# sufficient for a draft statement") to fully covered with no other change (issue
# #117). This mirrors issue #35 / LITAI-005's bounded retry for
# coding_cli.generated_metadata_invalid: two bounded extra attempts (three total)
# gives the model another chance to observe the same evidence with more confidence,
# while remaining bounded so a translation cannot retry forever. Unlike issue #35's
# malformed-JSON case, this is not a raised error -- inferred-intent is a legal
# claim kind -- so the retry is driven by inspecting the translation's own
# observations rather than catching an exception. Claims other than
# base-scope inferred-intent (suspected-defect, conflict, unknown, ...) are never
# retried here: those are the model's genuine, reproducible epistemic findings about
# the source (a real suspected defect, a real conflict) and retrying them away would
# silently suppress a real signal, exactly the failure mode issue #35's fix avoided
# for issue #39's genuinely persistent malformed metadata.
_INSUFFICIENT_EVIDENCE_RETRY_ATTEMPTS = 2


def _translation_has_insufficient_evidence(translation: ModelTranslation) -> bool:
    """Detect a translation with a retry-worthy inferred-intent shortfall.

    Only base-scope `inferred-intent` claims are retry-worthy: they are the model
    declining to commit to a stronger, evidence-backed claim kind for an in-scope
    surface, which issue #117 showed can simply be model-call flakiness. Every other
    claim kind (`suspected-defect`, `conflict`, `unknown`, and any Flavor-scoped
    claim) is left untouched, whether or not it ultimately renders as covered.
    """

    return any(
        observation.scope == "base"
        and observation.claim_kind is ClaimKind.INFERRED_INTENT
        for observation in translation.observations
    )


@dataclass(frozen=True, slots=True)
class ModelDerivation:
    result: SourceToSpecificationResult
    flavor_drafts: tuple[dict[str, Any], ...]
    stage_runs: tuple[dict[str, Any], ...]
    intelligence: SourceIntelligence
    journals: tuple[ModelCallJournal, ...]
    surface_inventory: BehavioralSurfaceInventory
    evidence_partition_manifest: EvidencePartitionManifest
    evidence_batch_plan: ModelEvidenceBatchPlan
    insufficient_evidence_retry_count: int = 0


def _reconcile_model_surfaces(
    independent: BehavioralSurfaceInventory,
    intelligence: SourceIntelligence,
    definitions: Iterable[ModelObservationDraft],
    translator_identity: str,
) -> BehavioralSurfaceInventory:
    exact_definitions = tuple(definitions)
    evidence_by_id = {
        item.reference.evidence_id: item.reference for item in intelligence.evidence
    }
    proposals = []
    for definition in exact_definitions:
        definition_evidence = frozenset(definition.evidence_ids)
        independently_mapped = any(
            definition_evidence
            & {reference.evidence_id for reference in surface.evidence}
            and observation_facet_maps_interface(
                definition.facet, surface.interface_kind
            )
            for surface in independent.surfaces
        )
        if not independently_mapped:
            proposals.append(
                create_model_surface_proposal(
                    translator_identity=translator_identity,
                    language=definition.language,
                    observation_id=definition.observation_id,
                    facet=definition.facet,
                    evidence=tuple(
                        evidence_by_id[item] for item in definition.evidence_ids
                    ),
                )
            )
    return reconcile_behavioral_surface_inventory(
        independent,
        translator_identity=translator_identity,
        observations={
            item.observation_id: (item.facet, item.evidence_ids)
            for item in exact_definitions
        },
        model_proposals=tuple(proposals),
    )


def _merge_component_graphs(
    graphs: tuple[ComponentGraphDraft, ...],
) -> ComponentGraphDraft:
    if not graphs:
        raise SourceToSpecificationError(
            "model_translation.component_graph_missing",
            "model translation emitted no Component graph",
        )
    snapshot = graphs[0].source_snapshot_id
    root = graphs[0].root_coordinate
    if any(
        item.source_snapshot_id != snapshot or item.root_coordinate != root
        for item in graphs
    ):
        raise SourceToSpecificationError(
            "model_translation.component_graph_binding_mismatch",
            "language Component graphs describe different source snapshots or roots",
        )
    node_parts: dict[str, dict[str, object]] = {}
    for graph in graphs:
        for node in graph.nodes:
            current = node_parts.setdefault(
                node.coordinate,
                {
                    "title": node.title,
                    "kind": node.kind,
                    "profiles": node.profiles,
                    "provided_capabilities": set(),
                    "capability_contracts": {},
                    "library_imports": {},
                    "entrypoints": {},
                    "build_needs": node.build_needs,
                    "source_paths": set(),
                    "observation_ids": set(),
                    "evidence_ids": set(),
                },
            )
            if current["title"] != node.title:
                raise SourceToSpecificationError(
                    "model_translation.component_graph_title_conflict",
                    f"language translators disagree on title for {node.coordinate}",
                )
            for field in ("kind", "profiles", "build_needs"):
                if current[field] != getattr(node, field):
                    raise SourceToSpecificationError(
                        "model_translation.component_graph_semantics_conflict",
                        "language translators disagree on "
                        f"{field} for {node.coordinate}",
                    )
            contracts = current["capability_contracts"]
            assert isinstance(contracts, dict)
            for contract in node.capability_contracts:
                part = contracts.setdefault(
                    contract.name,
                    {"contract": contract.contract, "evidence_ids": set()},
                )
                if part["contract"] != contract.contract:
                    raise SourceToSpecificationError(
                        "model_translation.component_graph_semantics_conflict",
                        "language translators disagree on capability contract "
                        f"{contract.name}",
                    )
                part["evidence_ids"].update(contract.evidence_ids)
            imports = current["library_imports"]
            assert isinstance(imports, dict)
            for item in node.library_imports:
                key = (item.language, item.capability)
                previous = imports.setdefault(key, item)
                if previous != item:
                    raise SourceToSpecificationError(
                        "model_translation.component_graph_semantics_conflict",
                        "language translators disagree on native import names",
                    )
            entrypoints = current["entrypoints"]
            assert isinstance(entrypoints, dict)
            for entrypoint in node.entrypoints:
                part = entrypoints.setdefault(
                    entrypoint.name,
                    {
                        "kind": entrypoint.kind,
                        "path": entrypoint.path,
                        "evidence_ids": set(),
                    },
                )
                if (part["kind"], part["path"]) != (
                    entrypoint.kind,
                    entrypoint.path,
                ):
                    raise SourceToSpecificationError(
                        "model_translation.component_graph_semantics_conflict",
                        "language translators disagree on entrypoint "
                        f"{entrypoint.name}",
                    )
                part["evidence_ids"].update(entrypoint.evidence_ids)
            for field in (
                "provided_capabilities",
                "source_paths",
                "observation_ids",
                "evidence_ids",
            ):
                values = current[field]
                assert isinstance(values, set)
                values.update(getattr(node, field))
    edge_parts: dict[tuple[str, str], dict[str, object]] = {}
    for graph in graphs:
        for edge in graph.edges:
            key = (edge.source_coordinate, edge.requirement_id)
            semantics = (
                edge.target_coordinate,
                edge.capability,
                edge.version_range,
                edge.dependency_kind,
                edge.optional,
            )
            current = edge_parts.setdefault(
                key, {"semantics": semantics, "evidence_ids": set()}
            )
            if current["semantics"] != semantics:
                raise SourceToSpecificationError(
                    "model_translation.component_graph_edge_conflict",
                    "language translators disagree on one Component requirement",
                )
            evidence_ids = current["evidence_ids"]
            assert isinstance(evidence_ids, set)
            evidence_ids.update(edge.evidence_ids)
    return ComponentGraphDraft(
        source_snapshot_id=snapshot,
        root_coordinate=root,
        nodes=tuple(
            ComponentGraphNodeDraft(
                coordinate=coordinate,
                title=str(value["title"]),
                kind=str(value["kind"]),
                profiles=value["profiles"],  # type: ignore[arg-type]
                provided_capabilities=tuple(
                    sorted(value["provided_capabilities"])  # type: ignore[arg-type]
                ),
                capability_contracts=tuple(
                    ComponentCapabilityContractDraft(
                        name=name,
                        contract=part["contract"],
                        evidence_ids=tuple(sorted(part["evidence_ids"])),
                    )
                    for name, part in sorted(value["capability_contracts"].items())  # type: ignore[union-attr]
                ),
                entrypoints=tuple(
                    ComponentEntrypointDraft(
                        name=name,
                        kind=part["kind"],
                        path=part["path"],
                        evidence_ids=tuple(sorted(part["evidence_ids"])),
                    )
                    for name, part in sorted(value["entrypoints"].items())  # type: ignore[union-attr]
                ),
                library_imports=tuple(
                    item for _key, item in sorted(value["library_imports"].items())
                ),
                build_needs=value["build_needs"],  # type: ignore[arg-type]
                source_paths=tuple(sorted(value["source_paths"])),  # type: ignore[arg-type]
                observation_ids=tuple(
                    sorted(value["observation_ids"])  # type: ignore[arg-type]
                ),
                evidence_ids=tuple(sorted(value["evidence_ids"])),  # type: ignore[arg-type]
            )
            for coordinate, value in sorted(node_parts.items())
        ),
        edges=tuple(
            ComponentGraphEdgeDraft(
                source_coordinate=source_coordinate,
                target_coordinate=value["semantics"][0],  # type: ignore[index,arg-type]
                requirement_id=requirement_id,
                capability=value["semantics"][1],  # type: ignore[index,arg-type]
                version_range=value["semantics"][2],  # type: ignore[index,arg-type]
                dependency_kind=value["semantics"][3],  # type: ignore[index,arg-type]
                optional=value["semantics"][4],  # type: ignore[index,arg-type]
                evidence_ids=tuple(sorted(value["evidence_ids"])),  # type: ignore[arg-type]
            )
            for (source_coordinate, requirement_id), value in sorted(edge_parts.items())
        ),
    )


def parse_model_observations(
    *,
    language: str,
    response: Mapping[str, object],
    skills: tuple[SpecAuthoringSkill, ...],
    evidence_ids: frozenset[str],
    behavioral_surfaces: tuple[BehavioralSurfaceInventoryItem, ...] = (),
) -> tuple[ModelObservationDraft, ...]:
    """Strictly admit a language translator response; unknown fields fail closed."""

    if set(response) != {"schema", "language", "observations", "component_graph"}:
        raise SourceToSpecificationError(
            "model_translation.output_fields_invalid",
            "model output fields must be exactly schema, language, observations, "
            "and component_graph",
        )
    if response.get("schema") != MODEL_TRANSLATION_OUTPUT_SCHEMA:
        raise SourceToSpecificationError(
            "model_translation.schema_unsupported",
            "model output uses an unsupported schema",
        )
    if response.get("language") != language:
        raise SourceToSpecificationError(
            "model_translation.language_mismatch",
            "model output describes another language",
        )
    raw_observations = response.get("observations")
    if not isinstance(raw_observations, list) or not raw_observations:
        raise SourceToSpecificationError(
            "model_translation.output_empty",
            "model output requires at least one observation",
        )
    skill_by_id = {item.skill_id: item for item in skills}
    expected_fields = {
        "skill_id",
        "facet",
        "claim_kind",
        "requirement",
        "capability",
        "scenario",
        "evidence_ids",
        "confidence_basis_points",
        "scope",
        "component_coordinate",
    }
    result: list[ModelObservationDraft] = []
    for index, value in enumerate(raw_observations):
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise SourceToSpecificationError(
                "model_translation.observation_fields_invalid",
                f"model observation {index} has missing or unknown fields",
            )
        skill_id = _text(value["skill_id"], f"observations[{index}].skill_id")
        skill = skill_by_id.get(skill_id)
        if skill is None:
            raise SourceToSpecificationError(
                "model_translation.skill_unselected",
                "model output names a skill outside the exact selected set",
            )
        facet = _text(value["facet"], f"observations[{index}].facet")
        if facet not in skill.facets:
            raise SourceToSpecificationError(
                "model_translation.skill_facet_mismatch",
                "model observation facet is not authorized by its exact skill",
            )
        scenario = value["scenario"]
        if not isinstance(scenario, dict) or set(scenario) != {"name", "when", "then"}:
            raise SourceToSpecificationError(
                "model_translation.scenario_invalid",
                "model observation scenario has missing or unknown fields",
            )
        selected_evidence = _model_string_set(
            value["evidence_ids"],
            f"observations[{index}].evidence_ids",
        )
        if not set(selected_evidence).issubset(evidence_ids):
            raise SourceToSpecificationError(
                "model_translation.evidence_unknown",
                "model output cites evidence outside the exact admitted bundle",
            )
        try:
            claim_kind = ClaimKind(value["claim_kind"])
        except (TypeError, ValueError) as exc:
            raise SourceToSpecificationError(
                "model_translation.claim_kind_invalid",
                "model observation claim kind is unsupported",
            ) from exc
        scope = _text(value["scope"], f"observations[{index}].scope")
        language_skill_id = LANGUAGE_SKILL_IDS[language]
        if (scope == "base" and skill_id == language_skill_id) or (
            scope == f"flavor:{language}" and skill_id != language_skill_id
        ):
            raise SourceToSpecificationError(
                "model_translation.skill_scope_mismatch",
                "common inverse skills may emit only base behavior and the exact "
                "language skill may emit only language-Flavor evidence",
            )
        result.append(
            ModelObservationDraft(
                language=language,
                skill_id=skill_id,
                facet=facet,
                claim_kind=claim_kind,
                requirement=_text(
                    value["requirement"], f"observations[{index}].requirement"
                ),
                capability=_text(
                    value["capability"], f"observations[{index}].capability"
                ),
                scenario=DraftScenario(
                    _text(scenario["name"], "scenario.name"),
                    _text(scenario["when"], "scenario.when"),
                    _text(scenario["then"], "scenario.then"),
                ),
                evidence_ids=selected_evidence,
                confidence_basis_points=value["confidence_basis_points"],  # type: ignore[arg-type]
                scope=scope,
                component_coordinate=_text(
                    value["component_coordinate"],
                    f"observations[{index}].component_coordinate",
                ),
            )
        )
    graph = response.get("component_graph")
    raw_nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    entrypoint_skill = skill_by_id.get("architecture")
    if isinstance(raw_nodes, list) and entrypoint_skill is not None:
        entrypoint_facet = next(
            (
                facet
                for facet in entrypoint_skill.facets
                if observation_facet_maps_interface(
                    facet, BehavioralInterfaceKind.ENTRYPOINT
                )
            ),
            None,
        )
        if entrypoint_facet is not None:
            for node_index, raw_node in enumerate(raw_nodes):
                if not isinstance(raw_node, Mapping):
                    continue
                coordinate = _text(
                    raw_node.get("coordinate"),
                    f"component_graph.nodes[{node_index}].coordinate",
                )
                raw_entrypoints = raw_node.get("entrypoints")
                if not isinstance(raw_entrypoints, list):
                    continue
                for entrypoint_index, raw_entrypoint in enumerate(raw_entrypoints):
                    if not isinstance(raw_entrypoint, Mapping):
                        continue
                    entrypoint_evidence = _model_string_set(
                        raw_entrypoint.get("evidence_ids"),
                        "component_graph.nodes"
                        f"[{node_index}].entrypoints[{entrypoint_index}].evidence_ids",
                        nonempty=True,
                    )
                    if not set(entrypoint_evidence).issubset(evidence_ids):
                        raise SourceToSpecificationError(
                            "model_translation.evidence_unknown",
                            "model Component entrypoint cites unknown evidence",
                        )
                    name = _text(
                        raw_entrypoint.get("name"),
                        "component entrypoint name",
                    )
                    path = _source_path(
                        raw_entrypoint.get("path"),
                        "component entrypoint path",
                    )
                    matching_surface_evidence = tuple(
                        sorted(
                            {
                                evidence.evidence_id
                                for surface in behavioral_surfaces
                                if surface.language == language
                                and surface.interface_kind
                                is BehavioralInterfaceKind.ENTRYPOINT
                                and surface.path == path
                                and surface.symbol == name
                                for evidence in surface.evidence
                            }
                        )
                    )
                    if matching_surface_evidence:
                        if not set(matching_surface_evidence).issubset(evidence_ids):
                            raise SourceToSpecificationError(
                                "model_translation.evidence_unknown",
                                "required Component entrypoint surface cites unknown "
                                "evidence",
                            )
                        entrypoint_evidence = matching_surface_evidence
                    if any(
                        observation.component_coordinate == coordinate
                        and set(observation.evidence_ids).intersection(
                            entrypoint_evidence
                        )
                        and observation_facet_maps_interface(
                            observation.facet, BehavioralInterfaceKind.ENTRYPOINT
                        )
                        for observation in result
                    ):
                        continue
                    kind = _text(
                        raw_entrypoint.get("kind"),
                        "component entrypoint kind",
                    )
                    result.append(
                        ModelObservationDraft(
                            language=language,
                            skill_id=entrypoint_skill.skill_id,
                            facet=entrypoint_facet,
                            claim_kind=ClaimKind.OBSERVED,
                            requirement=(
                                f"The Component exposes {name} as a {kind} "
                                f"entrypoint at {path}."
                            ),
                            capability=f"Entrypoint {name}",
                            scenario=DraftScenario(
                                f"Invoke {name}",
                                f"the {name} entrypoint is invoked",
                                f"the Component dispatches through {path}",
                            ),
                            evidence_ids=entrypoint_evidence,
                            confidence_basis_points=9000,
                            scope="base",
                            component_coordinate=coordinate,
                        )
                    )
    if len({item.observation_id for item in result}) != len(result):
        raise SourceToSpecificationError(
            "model_translation.observation_duplicate",
            "model output contains duplicate semantic observations",
        )
    if not any(
        item.scope == "base"
        and item.claim_kind in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
        for item in result
    ) or not any(item.scope == f"flavor:{language}" for item in result):
        raise SourceToSpecificationError(
            "model_translation.output_incomplete",
            "each language translator must emit observed base behavior and its "
            "language-Flavor evidence",
        )
    return tuple(result)


def _model_library_imports(value: object) -> tuple[AuthoredLibraryImport, ...]:
    try:
        if not isinstance(value, list) or len(value) > 256:
            raise ValueError("expected bounded array")
        return tuple(
            sorted(
                (AuthoredLibraryImport.from_dict(item) for item in value),
                key=lambda item: (item.language, item.capability),
            )
        )
    except (TypeError, ValueError) as exc:
        raise SourceToSpecificationError(
            "model_translation.component_graph_library_imports_invalid",
            "native imports must be bounded reviewed declarations",
        ) from exc


def parse_model_component_graph(
    *,
    response: Mapping[str, object],
    observations: tuple[ModelObservationDraft, ...],
    evidence_paths: Mapping[str, str],
    source_snapshot_id: str,
    root_coordinate: str,
) -> ComponentGraphDraft:
    """Admit a rooted graph whose every boundary is bound to exact evidence."""

    value = response.get("component_graph")
    required_graph_fields = {"nodes", "edges"}
    allowed_graph_fields = {*required_graph_fields, "root_coordinate"}
    if not isinstance(value, dict):
        raise SourceToSpecificationError(
            "model_translation.component_graph_invalid",
            "model Component graph must be an object",
        )
    graph_fields = set(value)
    missing_fields = required_graph_fields - graph_fields
    unknown_fields = graph_fields - allowed_graph_fields
    if missing_fields or unknown_fields:
        missing = ", ".join(sorted(missing_fields)) or "none"
        unknown = ", ".join(sorted(unknown_fields)) or "none"
        raise SourceToSpecificationError(
            "model_translation.component_graph_invalid",
            "model Component graph fields are invalid "
            f"(missing: {missing}; unknown: {unknown})",
        )
    declared_root_coordinate = value.get("root_coordinate", root_coordinate)
    if declared_root_coordinate != root_coordinate:
        raise SourceToSpecificationError(
            "model_translation.component_graph_root_mismatch",
            "model Component graph must preserve the exact requested root coordinate",
        )
    raw_nodes = value.get("nodes")
    raw_edges = value.get("edges")
    if (
        not isinstance(raw_nodes, list)
        or not raw_nodes
        or not isinstance(raw_edges, list)
    ):
        raise SourceToSpecificationError(
            "model_translation.component_graph_invalid",
            "model Component graph requires non-empty nodes and an edge array",
        )
    nodes: list[ComponentGraphNodeDraft] = []
    assigned_indexes: set[int] = set()
    declared_indexes: set[int] = set()
    for index, raw in enumerate(raw_nodes):
        expected = {
            "coordinate",
            "title",
            "kind",
            "profiles",
            "provided_capabilities",
            "capability_contracts",
            "entrypoints",
            "build_needs",
            "source_paths",
            "observation_indexes",
            "evidence_ids",
        }
        if not isinstance(raw, dict) or set(raw) - {"library_imports"} != expected:
            raise SourceToSpecificationError(
                "model_translation.component_graph_node_invalid",
                f"model Component graph node {index} has missing or unknown fields",
            )
        coordinate = _text(
            raw["coordinate"], f"component_graph.nodes[{index}].coordinate"
        )
        raw_indexes = raw["observation_indexes"]
        if (
            not isinstance(raw_indexes, list)
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 0 <= item < len(observations)
                for item in raw_indexes
            )
            or len(raw_indexes) != len(set(raw_indexes))
            or declared_indexes.intersection(raw_indexes)
        ):
            raise SourceToSpecificationError(
                "model_translation.component_graph_observations_invalid",
                "each model observation must belong to exactly one Component node",
            )
        if any(
            observations[item].component_coordinate != coordinate
            for item in raw_indexes
        ):
            raise SourceToSpecificationError(
                "model_translation.component_graph_observation_mismatch",
                "model observation Component coordinate differs from its graph node",
            )
        declared_indexes.update(raw_indexes)
        node_observation_indexes = tuple(
            observation_index
            for observation_index, observation in enumerate(observations)
            if observation.component_coordinate == coordinate
        )
        if not node_observation_indexes or assigned_indexes.intersection(
            node_observation_indexes
        ):
            raise SourceToSpecificationError(
                "model_translation.component_graph_observations_invalid",
                "each Component coordinate must own one unique observation set",
            )
        assigned_indexes.update(node_observation_indexes)
        evidence_ids = _model_string_set(
            raw["evidence_ids"],
            f"component_graph.nodes[{index}].evidence_ids",
            nonempty=True,
        )
        source_paths = _model_string_set(
            raw["source_paths"],
            f"component_graph.nodes[{index}].source_paths",
            nonempty=True,
        )
        if not set(evidence_ids).issubset(evidence_paths) or not set(
            source_paths
        ).issubset(evidence_paths.values()):
            raise SourceToSpecificationError(
                "model_translation.component_graph_evidence_unknown",
                "model Component graph node cites unavailable evidence or source paths",
            )
        observation_evidence = {
            evidence_id
            for observation_index in node_observation_indexes
            for evidence_id in observations[observation_index].evidence_ids
        }
        evidence_ids = tuple(sorted(set(evidence_ids) | observation_evidence))
        source_paths = tuple(
            sorted(
                set(source_paths)
                | {evidence_paths[evidence_id] for evidence_id in evidence_ids}
            )
        )
        _text(raw["title"], f"component_graph.nodes[{index}].title")
        provided_capabilities_path = (
            f"component_graph.nodes[{index}].provided_capabilities"
        )
        provided_capabilities = tuple(
            _portable_graph_identifier(
                item, f"{provided_capabilities_path}[{item_index}]"
            )
            for item_index, item in enumerate(
                _model_string_set(
                    raw["provided_capabilities"],
                    provided_capabilities_path,
                    nonempty=True,
                )
            )
        )
        raw_contracts = raw["capability_contracts"]
        if not isinstance(raw_contracts, list):
            raise SourceToSpecificationError(
                "model_translation.component_graph_capability_contract_invalid",
                "Component capability contracts must be an array",
            )
        capability_contracts: list[ComponentCapabilityContractDraft] = []
        for contract_index, contract_value in enumerate(raw_contracts):
            if not isinstance(contract_value, dict) or set(contract_value) != {
                "name",
                "contract",
                "evidence_ids",
            }:
                raise SourceToSpecificationError(
                    "model_translation.component_graph_capability_contract_invalid",
                    "Component capability contract has missing or unknown fields",
                )
            contract_evidence = _model_string_set(
                contract_value["evidence_ids"],
                f"component_graph.nodes[{index}].capability_contracts[{contract_index}].evidence_ids",
                nonempty=True,
            )
            if not set(contract_evidence).issubset(evidence_ids):
                raise SourceToSpecificationError(
                    "model_translation.component_graph_evidence_unknown",
                    "Component capability contract cites unavailable node evidence",
                )
            capability_contracts.append(
                ComponentCapabilityContractDraft(
                    name=_portable_graph_identifier(
                        contract_value["name"],
                        f"component_graph.nodes[{index}].capability_contracts[{contract_index}].name",
                    ),
                    contract=_text(
                        contract_value["contract"], "capability contract text"
                    ),
                    evidence_ids=contract_evidence,
                )
            )
        raw_entrypoints = raw["entrypoints"]
        if not isinstance(raw_entrypoints, list):
            raise SourceToSpecificationError(
                "model_translation.component_graph_entrypoint_invalid",
                "Component entrypoints must be an array",
            )
        entrypoints: list[ComponentEntrypointDraft] = []
        for entrypoint_index, entrypoint_value in enumerate(raw_entrypoints):
            if not isinstance(entrypoint_value, dict) or set(entrypoint_value) != {
                "name",
                "kind",
                "path",
                "evidence_ids",
            }:
                raise SourceToSpecificationError(
                    "model_translation.component_graph_entrypoint_invalid",
                    "Component entrypoint has missing or unknown fields",
                )
            entrypoint_evidence = _model_string_set(
                entrypoint_value["evidence_ids"],
                f"component_graph.nodes[{index}].entrypoints[{entrypoint_index}].evidence_ids",
                nonempty=True,
            )
            if not set(entrypoint_evidence).issubset(evidence_ids):
                raise SourceToSpecificationError(
                    "model_translation.component_graph_evidence_unknown",
                    "Component entrypoint cites unavailable node evidence",
                )
            entrypoints.append(
                ComponentEntrypointDraft(
                    name=_text(entrypoint_value["name"], "entrypoint name"),
                    kind=_text(entrypoint_value["kind"], "entrypoint kind"),
                    path=_source_path(entrypoint_value["path"], "entrypoint path"),
                    evidence_ids=entrypoint_evidence,
                )
            )
        node_kind = _text(raw["kind"], f"component_graph.nodes[{index}].kind")
        if node_kind not in {"library", "cli", "service"}:
            raise SourceToSpecificationError(
                "model_translation.component_graph_kind_unknown",
                "model Component kind must be exactly library, cli, or service",
            )
        raw_build_needs = _model_string_set(
            raw["build_needs"],
            f"component_graph.nodes[{index}].build_needs",
            nonempty=True,
        )
        language_need_prefixes = tuple(
            f"{observations[item].skill_id}."
            for item in node_observation_indexes
            if observations[item].scope.startswith("flavor:")
        )
        allowed_build_needs = {
            "implementation.language-ecosystem",
            "build.system",
            "platform.os",
        }
        language_need_aliases = {
            item
            for item in raw_build_needs
            if any(item.startswith(prefix) for prefix in language_need_prefixes)
        }
        unknown_build_needs = (
            set(raw_build_needs) - allowed_build_needs - language_need_aliases
        )
        if unknown_build_needs:
            raise SourceToSpecificationError(
                "model_translation.component_graph_build_need_invalid",
                "Component build needs must name exact Flavor axes",
            )
        build_needs = {
            (
                "implementation.language-ecosystem"
                if item in language_need_aliases
                else item
            )
            for item in raw_build_needs
        }
        if language_need_prefixes:
            build_needs.add("implementation.language-ecosystem")
        profiles_path = f"component_graph.nodes[{index}].profiles"
        profiles = {
            _portable_graph_identifier(item, f"{profiles_path}[{item_index}]")
            for item_index, item in enumerate(
                _model_string_set(raw["profiles"], profiles_path)
            )
        }
        if not profiles:
            profiles = {
                node_kind,
                *(
                    observations[item].scope.removeprefix("flavor:")
                    for item in node_observation_indexes
                    if observations[item].scope.startswith("flavor:")
                ),
            }
        nodes.append(
            ComponentGraphNodeDraft(
                coordinate=coordinate,
                title=_component_title(coordinate),
                kind=node_kind,
                profiles=tuple(sorted(profiles)),
                provided_capabilities=provided_capabilities,
                capability_contracts=tuple(
                    sorted(capability_contracts, key=lambda item: item.name)
                ),
                entrypoints=tuple(sorted(entrypoints, key=lambda item: item.name)),
                library_imports=_model_library_imports(raw.get("library_imports", [])),
                build_needs=tuple(sorted(build_needs)),
                source_paths=source_paths,
                observation_ids=tuple(
                    observations[item].observation_id
                    for item in node_observation_indexes
                ),
                evidence_ids=evidence_ids,
            )
        )
    if assigned_indexes != set(range(len(observations))):
        raise SourceToSpecificationError(
            "model_translation.component_graph_observations_incomplete",
            "every model observation must belong to exactly one Component node",
        )

    coordinates = {item.coordinate for item in nodes}
    edges: list[ComponentGraphEdgeDraft] = []
    for index, raw in enumerate(raw_edges):
        expected = {
            "source_coordinate",
            "target_coordinate",
            "requirement_id",
            "capability",
            "version_range",
            "dependency_kind",
            "optional",
            "evidence_ids",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise SourceToSpecificationError(
                "model_translation.component_graph_edge_invalid",
                f"model Component graph edge {index} has missing or unknown fields",
            )
        evidence_ids = _model_string_set(
            raw["evidence_ids"],
            f"component_graph.edges[{index}].evidence_ids",
            nonempty=True,
        )
        if not set(evidence_ids).issubset(evidence_paths):
            raise SourceToSpecificationError(
                "model_translation.component_graph_evidence_unknown",
                "model Component graph edge cites unavailable evidence",
            )
        source_coordinate = _text(
            raw["source_coordinate"],
            f"component_graph.edges[{index}].source_coordinate",
        )
        target_coordinate = _text(
            raw["target_coordinate"],
            f"component_graph.edges[{index}].target_coordinate",
        )
        if source_coordinate not in coordinates or target_coordinate not in coordinates:
            raise SourceToSpecificationError(
                "model_translation.component_graph_edge_unknown",
                "model Component graph edge references an unavailable node",
            )
        edges.append(
            ComponentGraphEdgeDraft(
                source_coordinate=source_coordinate,
                target_coordinate=target_coordinate,
                requirement_id=_portable_graph_identifier(
                    raw["requirement_id"],
                    f"component_graph.edges[{index}].requirement_id",
                ),
                capability=_portable_graph_identifier(
                    raw["capability"], f"component_graph.edges[{index}].capability"
                ),
                version_range=_text(
                    raw["version_range"],
                    f"component_graph.edges[{index}].version_range",
                ),
                dependency_kind=_text(
                    raw["dependency_kind"],
                    f"component_graph.edges[{index}].dependency_kind",
                ),
                optional=raw["optional"],  # type: ignore[arg-type]
                evidence_ids=evidence_ids,
            )
        )
    return ComponentGraphDraft(
        source_snapshot_id=source_snapshot_id,
        root_coordinate=root_coordinate,
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


def _exact_object(
    value: object, *, name: str, fields: frozenset[str]
) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SourceToSpecificationError(
            "model_translation.record_invalid", f"{name} must be an object"
        )
    missing = fields - value.keys()
    unknown = value.keys() - fields
    if missing or unknown:
        raise SourceToSpecificationError(
            "model_translation.record_invalid",
            f"{name} has missing or unknown fields",
        )
    return value


def _evidence_reference_from_dict(value: object) -> EvidenceReference:
    data = _exact_object(
        value,
        name="evidence reference",
        fields=frozenset(
            {"evidence_id", "source_snapshot_id", "content_digest", "path", "symbol"}
        ),
    )
    return EvidenceReference(
        evidence_id=_text(data["evidence_id"], "evidence_id"),
        source_snapshot_id=_text(data["source_snapshot_id"], "source_snapshot_id"),
        content_digest=_text(data["content_digest"], "content_digest"),
        path=_text(data["path"], "path"),
        symbol=(
            data["symbol"]
            if isinstance(data["symbol"], str)
            else _text(data["symbol"], "symbol")
        ),
    )


def _migrate_v2_source_intelligence(value: object) -> dict[str, object]:
    """Project the frozen v2 source-intelligence wire into the v3 neutral model."""

    legacy_fields = frozenset(
        {
            "schema",
            "authority_source_snapshot_id",
            "indexed_source_snapshot_id",
            "indexed_source_tree_id",
            "indexed_source_files",
            "source_content_identity",
            "source_material_identity",
            "index_evidence_identity",
            "provider_id",
            "provider_version",
            "runtime_version",
            "executable_identity",
            "database_identity",
            "built_with_version",
            "extraction_version",
            "document_count",
            "node_count",
            "edge_count",
            "languages",
            "evidence",
            "queries",
        }
    )
    data = _exact_object(value, name="v2 source intelligence", fields=legacy_fields)
    if data["schema"] != LEGACY_SOURCE_INTELLIGENCE_SCHEMA:
        raise SourceToSpecificationError(
            "model_translation.schema_unsupported",
            "source intelligence uses an unsupported schema",
        )
    built_with = _text(data["built_with_version"], "built_with_version")
    extraction_version = data["extraction_version"]
    if (
        isinstance(extraction_version, bool)
        or not isinstance(extraction_version, int)
        or extraction_version < 0
    ):
        raise SourceToSpecificationError(
            "model_translation.record_invalid",
            "v2 extraction_version must be a non-negative integer",
        )
    return {
        "schema": SOURCE_INTELLIGENCE_SCHEMA,
        "authority_source_snapshot_id": data["authority_source_snapshot_id"],
        "indexed_source_snapshot_id": data["indexed_source_snapshot_id"],
        "indexed_source_tree_id": data["indexed_source_tree_id"],
        "indexed_source_files": data["indexed_source_files"],
        "source_content_identity": data["source_content_identity"],
        "source_material_identity": data["source_material_identity"],
        "intelligence_evidence_identity": data["index_evidence_identity"],
        "provider_id": data["provider_id"],
        "provider_version": data["provider_version"],
        "runtime_version": data["runtime_version"],
        "executable_identity": data["executable_identity"],
        "artifact_identity": data["database_identity"],
        "artifact_media_type": "application/vnd.sqlite3",
        "capabilities": ["call-graph", "declarations", "references"],
        "provider_properties": [
            f"built-with-version={built_with}",
            f"extraction-version={extraction_version}",
        ],
        "document_count": data["document_count"],
        "symbol_count": data["node_count"],
        "relationship_count": data["edge_count"],
        "unresolved_relationship_count": 0,
        "warning_count": 0,
        "languages": data["languages"],
        "evidence": data["evidence"],
        "relationships": [],
        "unresolved_relationships": [],
        "queries": data["queries"],
    }


def source_intelligence_from_dict(value: object) -> SourceIntelligence:
    """Strictly parse a complete persisted provider-neutral evidence record."""

    legacy_identity: str | None = None
    if isinstance(value, Mapping) and value.get("schema") == (
        LEGACY_SOURCE_INTELLIGENCE_SCHEMA
    ):
        legacy_identity = canonical_digest(value)
        value = _migrate_v2_source_intelligence(value)

    fields = frozenset(
        {
            "schema",
            "authority_source_snapshot_id",
            "indexed_source_snapshot_id",
            "indexed_source_tree_id",
            "indexed_source_files",
            "source_content_identity",
            "source_material_identity",
            "intelligence_evidence_identity",
            "provider_id",
            "provider_version",
            "runtime_version",
            "executable_identity",
            "artifact_identity",
            "artifact_media_type",
            "capabilities",
            "provider_properties",
            "document_count",
            "symbol_count",
            "relationship_count",
            "unresolved_relationship_count",
            "warning_count",
            "languages",
            "evidence",
            "relationships",
            "unresolved_relationships",
            "queries",
        }
    )
    data = _exact_object(value, name="source intelligence", fields=fields)
    if data["schema"] != SOURCE_INTELLIGENCE_SCHEMA:
        raise SourceToSpecificationError(
            "model_translation.schema_unsupported",
            "source intelligence uses an unsupported schema",
        )
    raw_indexed_files = data["indexed_source_files"]
    if not isinstance(raw_indexed_files, list):
        raise SourceToSpecificationError(
            "model_translation.record_invalid",
            "indexed source file evidence must be an array",
        )
    indexed_source_files: list[tuple[str, int, str]] = []
    for raw in raw_indexed_files:
        item = _exact_object(
            raw,
            name="indexed source file",
            fields=frozenset({"path", "size", "identity"}),
        )
        indexed_source_files.append(
            (
                _text(item["path"], "indexed source path"),
                item["size"],  # type: ignore[arg-type]
                _text(item["identity"], "indexed source identity"),
            )
        )
    raw_evidence = data["evidence"]
    if not isinstance(raw_evidence, list):
        raise SourceToSpecificationError(
            "model_translation.record_invalid", "source evidence must be an array"
        )
    evidence: list[ModelEvidence] = []
    for raw in raw_evidence:
        item = _exact_object(
            raw,
            name="model evidence",
            fields=frozenset(
                {
                    "reference",
                    "language",
                    "kind",
                    "content",
                    "start_line",
                    "end_line",
                    "call_path",
                }
            ),
        )
        evidence.append(
            ModelEvidence(
                reference=_evidence_reference_from_dict(item["reference"]),
                language=_text(item["language"], "language"),
                kind=_text(item["kind"], "kind"),
                content=(
                    item["content"]
                    if isinstance(item["content"], str) and item["content"]
                    else _text(item["content"], "content")
                ),
                start_line=item["start_line"],  # type: ignore[arg-type]
                end_line=item["end_line"],  # type: ignore[arg-type]
                call_path=_string_tuple(item["call_path"], "call_path"),
            )
        )
    raw_relationships = data["relationships"]
    if not isinstance(raw_relationships, list):
        raise SourceToSpecificationError(
            "model_translation.record_invalid",
            "source relationships must be an array",
        )
    relationships: list[ModelRelationshipEvidence] = []
    for raw in raw_relationships:
        item = _exact_object(
            raw,
            name="model relationship evidence",
            fields=frozenset(
                {
                    "relationship_id",
                    "kind",
                    "source_symbol",
                    "source_path",
                    "source_language",
                    "target_symbol",
                    "target_path",
                    "target_language",
                    "line",
                    "column",
                    "provenance",
                    "resolution_method",
                    "confidence_basis_points",
                }
            ),
        )
        relationships.append(
            ModelRelationshipEvidence(
                relationship_id=_text(item["relationship_id"], "relationship_id"),
                kind=_text(item["kind"], "relationship kind"),
                source_symbol=_text(item["source_symbol"], "source symbol"),
                source_path=_text(item["source_path"], "source path"),
                source_language=_text(item["source_language"], "source language"),
                target_symbol=_text(item["target_symbol"], "target symbol"),
                target_path=_text(item["target_path"], "target path"),
                target_language=_text(item["target_language"], "target language"),
                line=item["line"],  # type: ignore[arg-type]
                column=item["column"],  # type: ignore[arg-type]
                provenance=_optional_text(item["provenance"], "provenance"),
                resolution_method=_optional_text(
                    item["resolution_method"], "resolution_method"
                ),
                confidence_basis_points=item[  # type: ignore[arg-type]
                    "confidence_basis_points"
                ],
            )
        )
    raw_unresolved = data["unresolved_relationships"]
    if not isinstance(raw_unresolved, list):
        raise SourceToSpecificationError(
            "model_translation.record_invalid",
            "unresolved source relationships must be an array",
        )
    unresolved_relationships: list[UnresolvedRelationshipEvidence] = []
    for raw in raw_unresolved:
        item = _exact_object(
            raw,
            name="unresolved relationship evidence",
            fields=frozenset(
                {
                    "unresolved_id",
                    "source_symbol",
                    "source_path",
                    "source_language",
                    "reference_name",
                    "reference_kind",
                    "line",
                    "column",
                    "candidates",
                }
            ),
        )
        unresolved_relationships.append(
            UnresolvedRelationshipEvidence(
                unresolved_id=_text(item["unresolved_id"], "unresolved_id"),
                source_symbol=_text(item["source_symbol"], "source symbol"),
                source_path=_text(item["source_path"], "source path"),
                source_language=_text(item["source_language"], "source language"),
                reference_name=_text(item["reference_name"], "reference name"),
                reference_kind=_text(item["reference_kind"], "reference kind"),
                line=item["line"],  # type: ignore[arg-type]
                column=item["column"],  # type: ignore[arg-type]
                candidates=_string_tuple(item["candidates"], "candidates"),
            )
        )
    raw_queries = data["queries"]
    if not isinstance(raw_queries, list):
        raise SourceToSpecificationError(
            "model_translation.record_invalid",
            "source-intelligence queries must be an array",
        )
    queries: list[IntelligenceQueryRecord] = []
    for raw in raw_queries:
        item = _exact_object(
            raw,
            name="source-intelligence query",
            fields=frozenset({"query_id", "language", "text", "evidence_ids"}),
        )
        queries.append(
            IntelligenceQueryRecord(
                _text(item["query_id"], "query_id"),
                _text(item["language"], "language"),
                _text(item["text"], "query text"),
                _string_tuple(item["evidence_ids"], "query evidence IDs"),
            )
        )
    return SourceIntelligence(
        authority_source_snapshot_id=_text(
            data["authority_source_snapshot_id"], "authority_source_snapshot_id"
        ),
        indexed_source_snapshot_id=_text(
            data["indexed_source_snapshot_id"], "indexed_source_snapshot_id"
        ),
        indexed_source_tree_id=_text(
            data["indexed_source_tree_id"], "indexed_source_tree_id"
        ),
        indexed_source_files=tuple(indexed_source_files),
        source_content_identity=_text(
            data["source_content_identity"], "source_content_identity"
        ),
        source_material_identity=_text(
            data["source_material_identity"], "source_material_identity"
        ),
        intelligence_evidence_identity=_text(
            data["intelligence_evidence_identity"],
            "intelligence_evidence_identity",
        ),
        provider_id=_text(data["provider_id"], "provider_id"),
        provider_version=_text(data["provider_version"], "provider_version"),
        runtime_version=_text(data["runtime_version"], "runtime_version"),
        executable_identity=_text(data["executable_identity"], "executable_identity"),
        artifact_identity=_text(data["artifact_identity"], "artifact_identity"),
        artifact_media_type=_text(data["artifact_media_type"], "artifact_media_type"),
        capabilities=_string_tuple(data["capabilities"], "capabilities"),
        provider_properties=_string_tuple(
            data["provider_properties"], "provider_properties"
        ),
        document_count=data["document_count"],  # type: ignore[arg-type]
        symbol_count=data["symbol_count"],  # type: ignore[arg-type]
        relationship_count=data["relationship_count"],  # type: ignore[arg-type]
        unresolved_relationship_count=data[  # type: ignore[arg-type]
            "unresolved_relationship_count"
        ],
        warning_count=data["warning_count"],  # type: ignore[arg-type]
        languages=_string_tuple(data["languages"], "languages", nonempty=True),
        evidence=tuple(evidence),
        relationships=tuple(relationships),
        unresolved_relationships=tuple(unresolved_relationships),
        queries=tuple(queries),
        record_identity=legacy_identity,
    )


def model_call_journal_from_dict(value: object) -> ModelCallJournal:
    """Strictly parse and cryptographically rebind a complete model-call journal."""

    legacy_fields = frozenset(
        {
            "schema",
            "call_id",
            "language",
            "coding_cli",
            "executable",
            "executable_identity",
            "model",
            "command",
            "prompt",
            "response",
            "response_text",
            "stdout",
            "stderr",
            "request_identity",
            "response_identity",
            "command_identity",
            "selection_identity",
            "tool_binding_identity",
            "isolation",
            "environment_keys",
            "skill_refs",
            "intelligence_identity",
            "egress_policy_id",
        }
    )
    if not isinstance(value, Mapping):
        raise SourceToSpecificationError(
            "model_translation.record_invalid", "model call journal must be an object"
        )
    record_schema = value.get("schema")
    fields = (
        legacy_fields
        if record_schema == LEGACY_MODEL_CALL_JOURNAL_SCHEMA
        else legacy_fields | {"evidence_ids", "partition_ordinal", "partition_count"}
    )
    data = _exact_object(value, name="model call journal", fields=fields)
    if record_schema not in {
        MODEL_CALL_JOURNAL_SCHEMA,
        PREVIOUS_MODEL_CALL_JOURNAL_SCHEMA,
        LEGACY_MODEL_CALL_JOURNAL_SCHEMA,
    }:
        raise SourceToSpecificationError(
            "model_translation.schema_unsupported",
            "model call journal uses an unsupported schema",
        )
    response = data["response"]
    if not isinstance(response, dict):
        raise SourceToSpecificationError(
            "model_translation.record_invalid", "journal response must be an object"
        )
    response_text = (
        data["response_text"]
        if isinstance(data["response_text"], str) and data["response_text"]
        else _text(data["response_text"], "response_text")
    )
    try:
        decoded_response = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise SourceToSpecificationError(
            "model_translation.record_invalid",
            "journal response text must contain its exact JSON response",
        ) from exc
    if decoded_response != response:
        raise SourceToSpecificationError(
            "model_translation.journal_response_mismatch",
            "journal response text does not match its decoded response",
        )
    raw_skills = data["skill_refs"]
    if not isinstance(raw_skills, list):
        raise SourceToSpecificationError(
            "model_translation.record_invalid", "journal skills must be an array"
        )
    skills = tuple(
        SkillRef(
            _text(item["skill_id"], "skill_id"),
            _text(item["version"], "version"),
            _text(item["content_digest"], "content_digest"),
        )
        for raw in raw_skills
        for item in (
            _exact_object(
                raw,
                name="journal skill reference",
                fields=frozenset({"skill_id", "version", "content_digest"}),
            ),
        )
    )
    isolation = _exact_object(
        data["isolation"],
        name="coding CLI isolation",
        fields=frozenset(
            {
                "profile",
                "clean_configuration",
                "filesystem_boundary",
                "hermetic",
                "limitations",
            }
        ),
    )
    if (
        not isinstance(isolation["clean_configuration"], bool)
        or not isinstance(isolation["hermetic"], bool)
        or not isinstance(data["stdout"], str)
        or not isinstance(data["stderr"], str)
        or data["model"] is not None
        and not isinstance(data["model"], str)
    ):
        raise SourceToSpecificationError(
            "model_translation.record_invalid", "journal field types are invalid"
        )
    journal = ModelCallJournal(
        call_id=_text(data["call_id"], "call_id"),
        language=_text(data["language"], "language"),
        coding_cli=_text(data["coding_cli"], "coding_cli"),
        executable=_text(data["executable"], "executable"),
        executable_identity=_text(data["executable_identity"], "executable_identity"),
        model=data["model"],  # type: ignore[arg-type]
        command=_string_tuple(data["command"], "command", nonempty=True),
        prompt=(
            data["prompt"]
            if isinstance(data["prompt"], str) and data["prompt"]
            else _text(data["prompt"], "prompt")
        ),
        response=response,
        response_text=response_text,
        stdout=data["stdout"],
        stderr=data["stderr"],
        request_identity=_text(data["request_identity"], "request_identity"),
        response_identity=_text(data["response_identity"], "response_identity"),
        command_identity=_text(data["command_identity"], "command_identity"),
        selection_identity=_text(data["selection_identity"], "selection_identity"),
        tool_binding_identity=_text(
            data["tool_binding_identity"], "tool_binding_identity"
        ),
        isolation={
            "profile": _text(isolation["profile"], "isolation.profile"),
            "clean_configuration": isolation["clean_configuration"],
            "filesystem_boundary": _text(
                isolation["filesystem_boundary"], "isolation.filesystem_boundary"
            ),
            "hermetic": isolation["hermetic"],
            "limitations": list(
                _string_tuple(isolation["limitations"], "isolation.limitations")
            ),
        },
        environment_keys=_string_tuple(data["environment_keys"], "environment_keys"),
        skill_refs=skills,
        intelligence_identity=_text(
            data["intelligence_identity"], "intelligence_identity"
        ),
        egress_policy_id=_text(data["egress_policy_id"], "egress_policy_id"),
        evidence_ids=(
            ()
            if record_schema == LEGACY_MODEL_CALL_JOURNAL_SCHEMA
            else _string_tuple(
                data["evidence_ids"], "journal evidence IDs", nonempty=True
            )
        ),
        partition_ordinal=(
            0
            if record_schema == LEGACY_MODEL_CALL_JOURNAL_SCHEMA
            else data["partition_ordinal"]  # type: ignore[arg-type]
        ),
        partition_count=(
            1
            if record_schema == LEGACY_MODEL_CALL_JOURNAL_SCHEMA
            else data["partition_count"]  # type: ignore[arg-type]
        ),
        record_schema=record_schema,
    )
    expected_identities = {
        "request_identity": canonical_digest(journal.prompt),
        "response_identity": canonical_digest(journal.response),
        "command_identity": canonical_digest(journal.command),
        "selection_identity": canonical_digest(
            {
                "coding_cli": journal.coding_cli,
                "executable": journal.executable,
                "executable_identity": journal.executable_identity,
                "isolation": canonical_value(journal.isolation),
            }
        ),
        "tool_binding_identity": canonical_digest(
            {
                "schema": "literate-ai/coding-cli-tool-binding@1",
                "coding_cli": journal.coding_cli,
                "executable_identity": journal.executable_identity,
                "isolation": canonical_value(journal.isolation),
            }
        ),
    }
    if any(
        getattr(journal, field) != expected
        for field, expected in expected_identities.items()
    ):
        raise SourceToSpecificationError(
            "model_translation.journal_binding_mismatch",
            "model call journal transcript identities do not match their content",
        )
    return journal


def validate_inverse_evidence_custody(
    *,
    value: object,
    translation: object,
) -> str:
    """Rebuild the retained inverse admission plan without trusting acceptance.

    Qualification uses this narrower verifier before executing generated code.  It
    proves that the exact retained source inventory was fully dispositioned, every
    admitted evidence item fit into one deterministic bounded model call, and the
    persisted journals followed that call plan exactly.
    """

    custody = _exact_object(
        value,
        name="inverse evidence custody",
        fields=frozenset(
            {
                "schema",
                "translation_identity",
                "source_inventory",
                "behavioral_surface_inventory",
                "evidence_partition_manifest",
                "evidence_batch_plan",
            }
        ),
    )
    if custody["schema"] != INVERSE_EVIDENCE_CUSTODY_SCHEMA:
        raise SourceToSpecificationError(
            "inverse_evidence.schema_invalid",
            "inverse evidence custody uses an unsupported schema",
        )
    translation_record = _exact_object(
        translation,
        name="translation run",
        fields=frozenset({"schema", "mode", "intelligence", "journals"}),
    )
    if (
        translation_record["schema"] != SOURCE_TRANSLATION_RUN_SCHEMA
        or translation_record["mode"] != MODEL_TRANSLATION_MODE
    ):
        raise SourceToSpecificationError(
            "inverse_evidence.translation_invalid",
            "inverse evidence custody requires a current semantic translation",
        )
    translation_identity = canonical_digest(translation_record)
    if custody["translation_identity"] != translation_identity:
        raise SourceToSpecificationError(
            "inverse_evidence.translation_mismatch",
            "inverse evidence custody names a different translation",
        )

    intelligence = source_intelligence_from_dict(translation_record["intelligence"])
    inventory = source_inventory_from_dict(custody["source_inventory"])
    retained_partition = EvidencePartitionManifest.from_dict(
        custody["evidence_partition_manifest"]
    )
    expected_partition = build_evidence_partition_manifest(inventory, intelligence)
    expected_partition.require_complete()
    if retained_partition != expected_partition:
        raise SourceToSpecificationError(
            "inverse_evidence.partition_mismatch",
            "retained inverse partition differs from exact inventory and evidence",
        )

    retained_plan = ModelEvidenceBatchPlan.from_dict(custody["evidence_batch_plan"])
    expected_plan = plan_model_evidence_batches(
        intelligence,
        languages=intelligence.languages,
        maximum_batch_bytes=retained_plan.maximum_batch_bytes,
    )
    if retained_plan != expected_plan:
        raise SourceToSpecificationError(
            "inverse_evidence.batch_plan_mismatch",
            "retained inverse batch plan differs from exact admitted evidence",
        )

    raw_journals = translation_record["journals"]
    if not isinstance(raw_journals, list):
        raise SourceToSpecificationError(
            "inverse_evidence.journals_invalid",
            "semantic translation journals must be an array",
        )
    journals = tuple(model_call_journal_from_dict(item) for item in raw_journals)
    journal_plan = tuple(
        (
            item.language,
            item.partition_ordinal,
            item.partition_count,
            item.evidence_ids,
        )
        for item in journals
    )
    retained_journal_plan = tuple(
        (
            item.language,
            item.language_ordinal,
            item.language_batch_count,
            item.evidence_ids,
        )
        for item in retained_plan.batches
    )
    if journal_plan != retained_journal_plan:
        raise SourceToSpecificationError(
            "inverse_evidence.journal_plan_mismatch",
            "semantic journals do not exactly implement the retained batch plan",
        )

    retained_surfaces = BehavioralSurfaceInventory.from_dict(
        custody["behavioral_surface_inventory"]
    )
    retained_surfaces.require_complete()
    if retained_surfaces.source_snapshot_identity != inventory.identity:
        raise SourceToSpecificationError(
            "inverse_evidence.surface_inventory_mismatch",
            "behavioral inventory describes a different source snapshot",
        )
    return canonical_digest(custody)


def validate_qualification_inverse_evidence(
    *, translation: object | None, custody: object | None
) -> str | None:
    """Require semantic inverse custody while explicitly exempting static runs."""

    if translation is None:
        if custody is not None:
            raise SourceToSpecificationError(
                "inverse_evidence.orphaned",
                "inverse evidence custody cannot exist without a translation",
            )
        return None
    record = _exact_object(
        translation,
        name="translation run",
        fields=frozenset({"schema", "mode", "intelligence", "journals"}),
    )
    if record["mode"] == "deterministic-static":
        if (
            record["schema"] != SOURCE_TRANSLATION_RUN_SCHEMA
            or record["intelligence"] is not None
            or record["journals"] != []
            or custody is not None
        ):
            raise SourceToSpecificationError(
                "inverse_evidence.static_invalid",
                "static translation must contain no model or inverse custody evidence",
            )
        return None
    if record["mode"] in {MODEL_TRANSLATION_MODE, LEGACY_MODEL_TRANSLATION_MODE}:
        if custody is None:
            raise SourceToSpecificationError(
                "inverse_evidence.required",
                "semantic qualification requires retained inverse evidence custody",
            )
        return validate_inverse_evidence_custody(
            value=custody,
            translation=record,
        )
    raise SourceToSpecificationError(
        "inverse_evidence.translation_invalid",
        "qualification translation mode is unsupported",
    )


def validate_model_translation_record(
    *,
    value: object,
    result: SourceToSpecificationResult,
    skill_catalog: Mapping[str, SpecAuthoringSkill],
    surface_inventory: object | None = None,
    evidence_partition_manifest: object | None = None,
    evidence_batch_plan: object | None = None,
    source_inventory: object | None = None,
) -> tuple[dict[str, Any], ...]:
    """Verify a persisted semantic translation and rederive its Flavor proposals."""

    data = _exact_object(
        value,
        name="translation run",
        fields=frozenset({"schema", "mode", "intelligence", "journals"}),
    )
    accepted_run_bindings = {
        (SOURCE_TRANSLATION_RUN_SCHEMA, MODEL_TRANSLATION_MODE),
        (PREVIOUS_SOURCE_TRANSLATION_RUN_SCHEMA, MODEL_TRANSLATION_MODE),
        (LEGACY_SEMANTIC_SOURCE_TRANSLATION_RUN_SCHEMA, MODEL_TRANSLATION_MODE),
        (LEGACY_SOURCE_TRANSLATION_RUN_SCHEMA, LEGACY_MODEL_TRANSLATION_MODE),
    }
    if (data["schema"], data["mode"]) not in accepted_run_bindings:
        raise SourceToSpecificationError(
            "model_translation.record_invalid",
            "semantic translation record schema or mode is invalid",
        )
    if data["schema"] == SOURCE_TRANSLATION_RUN_SCHEMA and evidence_batch_plan is None:
        raise SourceToSpecificationError(
            "evidence_batch.missing",
            "current semantic translation requires its evidence batch plan",
        )
    intelligence = source_intelligence_from_dict(data["intelligence"])
    if evidence_partition_manifest is not None:
        if source_inventory is None:
            raise SourceToSpecificationError(
                "evidence_partition.inventory_missing",
                "partition verification requires the retained source inventory",
            )
        retained_partition = EvidencePartitionManifest.from_dict(
            evidence_partition_manifest
        )
        expected_partition = build_evidence_partition_manifest(
            source_inventory_from_dict(source_inventory), intelligence
        )
        expected_partition.require_complete()
        if retained_partition != expected_partition:
            raise SourceToSpecificationError(
                "evidence_partition.record_binding_mismatch",
                "retained partition manifest differs from exact inventory and evidence",
            )
    retained_batch_plan: ModelEvidenceBatchPlan | None = None
    if evidence_batch_plan is not None:
        retained_batch_plan = ModelEvidenceBatchPlan.from_dict(evidence_batch_plan)
        expected_batch_plan = plan_model_evidence_batches(
            intelligence,
            languages=intelligence.languages,
            maximum_batch_bytes=retained_batch_plan.maximum_batch_bytes,
        )
        if retained_batch_plan != expected_batch_plan:
            raise SourceToSpecificationError(
                "evidence_batch.record_binding_mismatch",
                "retained batch plan differs from exact admitted evidence",
            )
    expected_skill_set = builtin_skill_set(
        skill_catalog, languages=intelligence.languages
    )
    resolved_skills = resolve_skill_set(expected_skill_set, skill_catalog)
    if (
        expected_skill_set.identity != result.run.skill_set_identity
        or expected_skill_set.skill_set_id != result.request.skill_set_id
    ):
        raise SourceToSpecificationError(
            "model_translation.skill_set_mismatch",
            "translation result does not bind the exact reviewed inverse skill set",
        )
    raw_journals = data["journals"]
    if not isinstance(raw_journals, list):
        raise SourceToSpecificationError(
            "model_translation.record_invalid", "translation journals must be an array"
        )
    journals = tuple(model_call_journal_from_dict(item) for item in raw_journals)
    if (
        intelligence.source_snapshot_id != result.request.source_snapshot_id
        or tuple(sorted({item.language for item in journals})) != intelligence.languages
        or len({item.call_id for item in journals}) != len(journals)
        or any(
            item.intelligence_identity != intelligence.identity
            or item.egress_policy_id != result.request.egress_policy_id
            for item in journals
        )
    ):
        raise SourceToSpecificationError(
            "model_translation.record_binding_mismatch",
            "translation record does not bind the exact result and intelligence",
        )
    evidence_ids_by_language = {
        language: frozenset(
            item.reference.evidence_id
            for item in intelligence.evidence
            if item.language == language
        )
        for language in intelligence.languages
    }
    journals_by_language: dict[str, list[ModelCallJournal]] = {}
    for journal in journals:
        journals_by_language.setdefault(journal.language, []).append(journal)
    for language, language_journals in journals_by_language.items():
        ordered = sorted(language_journals, key=lambda item: item.partition_ordinal)
        legacy = all(
            item.record_schema == LEGACY_MODEL_CALL_JOURNAL_SCHEMA for item in ordered
        )
        if legacy:
            if len(ordered) != 1:
                raise SourceToSpecificationError(
                    "model_translation.partition_binding_mismatch",
                    "legacy language translation requires exactly one journal",
                )
            continue
        admitted_ids = tuple(
            evidence_id for item in ordered for evidence_id in item.evidence_ids
        )
        if (
            tuple(item.partition_ordinal for item in ordered)
            != tuple(range(len(ordered)))
            or any(item.partition_count != len(ordered) for item in ordered)
            or len(admitted_ids) != len(set(admitted_ids))
            or frozenset(admitted_ids) != evidence_ids_by_language[language]
        ):
            raise SourceToSpecificationError(
                "model_translation.partition_binding_mismatch",
                "model journals do not exactly partition language evidence",
            )
    if retained_batch_plan is not None and tuple(
        (
            item.language,
            item.partition_ordinal,
            item.partition_count,
            item.evidence_ids,
        )
        for item in journals
    ) != tuple(
        (
            item.language,
            item.language_ordinal,
            item.language_batch_count,
            item.evidence_ids,
        )
        for item in retained_batch_plan.batches
    ):
        raise SourceToSpecificationError(
            "evidence_batch.journal_binding_mismatch",
            "model journals do not match the retained evidence batch plan",
        )
    definitions: list[ModelObservationDraft] = []
    graphs: list[ComponentGraphDraft] = []
    call_by_observation: dict[str, str] = {}
    independent_surfaces = collect_behavioral_surface_inventory(intelligence)
    for journal in journals:
        expected_refs = tuple(
            item.ref
            for item in resolved_skills
            if not item.skill_id.startswith("language-")
            or item.skill_id == LANGUAGE_SKILL_IDS[journal.language]
        )
        if journal.skill_refs != expected_refs:
            raise SourceToSpecificationError(
                "model_translation.skill_selection_mismatch",
                "journal does not name the exact common and language inverse skills",
            )
        selected_skills: list[SpecAuthoringSkill] = []
        for reference in journal.skill_refs:
            skill = skill_catalog.get(reference.skill_id)
            if skill is None or skill.ref != reference:
                raise SourceToSpecificationError(
                    "model_translation.skill_identity_mismatch",
                    "journal names an unavailable or changed inverse skill",
                )
            selected_skills.append(skill)
        admitted_evidence_ids = (
            evidence_ids_by_language[journal.language]
            if journal.record_schema == LEGACY_MODEL_CALL_JOURNAL_SCHEMA
            else frozenset(journal.evidence_ids)
        )
        parsed = parse_model_observations(
            language=journal.language,
            response=journal.response,
            skills=tuple(selected_skills),
            evidence_ids=admitted_evidence_ids,
            behavioral_surfaces=tuple(
                surface
                for surface in independent_surfaces.surfaces
                if surface.language == journal.language
                and any(
                    evidence.evidence_id in admitted_evidence_ids
                    for evidence in surface.evidence
                )
            ),
        )
        graph = parse_model_component_graph(
            response=journal.response,
            observations=parsed,
            evidence_paths={
                item.reference.evidence_id: item.reference.path
                for item in intelligence.evidence
                if item.language == journal.language
                and item.reference.evidence_id in admitted_evidence_ids
            },
            source_snapshot_id=intelligence.source_snapshot_id,
            root_coordinate=(
                result.component_graph_draft.root_coordinate
                if result.component_graph_draft is not None
                else ""
            ),
        )
        definitions.extend(parsed)
        graphs.append(graph)
        call_by_observation.update(
            {item.observation_id: journal.call_id for item in parsed}
        )
    definition_by_id = {item.observation_id: item for item in definitions}
    observation_by_id = {item.observation_id: item for item in result.observations}
    if definition_by_id.keys() != observation_by_id.keys():
        raise SourceToSpecificationError(
            "model_translation.observation_binding_mismatch",
            "journal observations do not exactly match the derived result",
        )
    merged_graph = _merge_component_graphs(tuple(graphs))
    if result.component_graph_draft != merged_graph:
        raise SourceToSpecificationError(
            "model_translation.component_graph_binding_mismatch",
            "journal Component graphs do not exactly match the derived result",
        )
    evidence_by_id = {
        item.reference.evidence_id: item.reference for item in intelligence.evidence
    }
    for observation_id, definition in definition_by_id.items():
        observation = observation_by_id[observation_id]
        if (
            observation.surface_ids
            != ("model-surface:" + observation_id.removeprefix("model-observation:"),)
            or observation.facet != definition.facet
            or observation.claim_kind is not definition.claim_kind
            or observation.statement != definition.requirement
            or observation.skill.skill_id != definition.skill_id
            or observation.confidence != definition.confidence_basis_points / 10_000
            or observation.model_call_id != call_by_observation[observation_id]
            or observation.evidence
            != tuple(evidence_by_id[item] for item in definition.evidence_ids)
        ):
            raise SourceToSpecificationError(
                "model_translation.observation_binding_mismatch",
                "journal observation content does not match the derived result",
            )
    if surface_inventory is not None:
        retained_inventory = BehavioralSurfaceInventory.from_dict(surface_inventory)
        translator_identity = canonical_digest(
            {
                "schema": "literate-ai/inverse-translator-set@1",
                "calls": [
                    {
                        "call_id": item.call_id,
                        "selection_identity": item.selection_identity,
                        "tool_binding_identity": item.tool_binding_identity,
                    }
                    for item in journals
                ],
            }
        )
        expected_inventory = _reconcile_model_surfaces(
            collect_behavioral_surface_inventory(intelligence),
            intelligence,
            definitions,
            translator_identity,
        )
        expected_inventory.require_complete()
        if retained_inventory != expected_inventory:
            raise SourceToSpecificationError(
                "surface_inventory.record_binding_mismatch",
                "retained surface inventory differs from exact evidence and journals",
            )
    expected_request_id = canonical_digest(
        {
            "source_snapshot_id": result.request.source_snapshot_id,
            "origin_attestation_id": result.request.origin_attestation_id,
            "skill_set_identity": result.run.skill_set_identity,
            "intelligence_identity": intelligence.identity,
            "model_call_ids": tuple(item.call_id for item in journals),
            **(
                {"evidence_batch_plan_identity": retained_batch_plan.identity}
                if data["schema"] == SOURCE_TRANSLATION_RUN_SCHEMA
                and retained_batch_plan is not None
                else {}
            ),
            "component_graph_identity": merged_graph.identity,
            "output_provider": result.request.output_provider,
            "mode": result.request.mode.value,
            "previous_specification_set_id": (
                result.request.previous_specification_set_id
            ),
        }
    )
    if result.request.request_id != expected_request_id:
        raise SourceToSpecificationError(
            "model_translation.request_binding_mismatch",
            "translation record does not reproduce the signed draft request",
        )
    return _flavor_drafts(tuple(definitions))


def _flavor_drafts(
    definitions: tuple[ModelObservationDraft, ...],
) -> tuple[dict[str, Any], ...]:
    drafts: list[dict[str, Any]] = []
    for language in sorted({item.language for item in definitions}):
        selected = tuple(
            item
            for item in definitions
            if item.scope == f"flavor:{language}"
            and item.claim_kind in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
        )
        if not selected:
            continue
        statements = tuple(
            DraftStatement(
                statement_id=f"flavor-statement:{item.observation_id}",
                capability=item.capability,
                requirement=item.requirement,
                scenarios=(item.scenario,),
                observation_ids=(item.observation_id,),
            )
            for item in selected
        )
        payload = {
            "flavor_id": language,
            "axis": "implementation.language-ecosystem",
            "observation_ids": tuple(item.observation_id for item in selected),
            "statements": statements,
        }
        drafts.append(
            {
                "schema": "literate-ai/flavor-draft-set@1",
                "flavor_draft_id": canonical_digest(payload),
                "flavor_id": language,
                "axis": "implementation.language-ecosystem",
                "observation_ids": list(payload["observation_ids"]),
                "statements": canonical_value(statements),
                "status": "proposal",
            }
        )
    return tuple(drafts)


def derive_model_checkout(
    *,
    source: str | Path,
    inventory: SourceInventory,
    origin_attestation_id: str,
    skill_set: SpecAuthoringSkillSet,
    skill_catalog: dict[str, SpecAuthoringSkill],
    intelligence_collector: SourceIntelligenceCollector,
    translator: InverseLanguageTranslator,
    mode: RunMode = RunMode.BOOTSTRAP,
    previous_specification_set_id: str | None = None,
    egress_policy_id: str = "explicit-source-evidence-to-selected-coding-cli@1",
    trusted_skill_classifications: tuple[str, ...] = ("builtin-reviewed",),
    maximum_model_evidence_bytes: int = 262_144,
) -> ModelDerivation:
    """Translate admitted source evidence through one selected model call per
    language."""

    selected_path = Path(source).resolve()
    root = selected_path if selected_path.is_dir() else selected_path.parent
    root_coordinate = f"local/{component_name(root)}"
    raw_languages = {
        item.language for item in inventory.entries if item.language is not None
    }
    if "cpp" in raw_languages:
        # Conventional .h files are lexically ambiguous. Once a checkout contains a
        # C++ translation unit, treat its C-classified companions as part of that C++
        # implementation rather than rejecting an otherwise supported mixed tree.
        raw_languages.discard("c")
    languages = tuple(
        sorted(
            {
                "javascript" if item == "typescript" else item
                for item in raw_languages
                if item in LANGUAGE_SKILL_IDS
            }
        )
    )
    unsupported = tuple(sorted(raw_languages - set(LANGUAGE_SKILL_IDS)))
    if unsupported or not languages:
        raise SourceToSpecificationError(
            "model_translation.language_unsupported",
            "model-backed source translation requires Python, C++, Rust, or "
            "JavaScript/TypeScript source",
        )
    ordered_skills = resolve_skill_set(skill_set, skill_catalog)
    untrusted_skills = tuple(
        item.skill_id
        for item in ordered_skills
        if item.trust_classification not in trusted_skill_classifications
    )
    if untrusted_skills:
        raise SourceToSpecificationError(
            "workflow.skill_untrusted",
            "model translation is not authorized for skill trust classification: "
            + ", ".join(untrusted_skills),
        )
    guard = SourceTreeFingerprint(root)
    intelligence = intelligence_collector.collect(
        source=selected_path,
        inventory=inventory,
        languages=languages,
    )
    guard.require_unchanged()
    if intelligence.source_snapshot_id != inventory.identity:
        raise SourceToSpecificationError(
            "model_translation.intelligence_snapshot_mismatch",
            "source-intelligence evidence describes another source snapshot",
        )
    partition_manifest = build_evidence_partition_manifest(inventory, intelligence)
    partition_manifest.require_complete()
    batch_plan = plan_model_evidence_batches(
        intelligence,
        languages=languages,
        maximum_batch_bytes=maximum_model_evidence_bytes,
    )
    independent_surfaces = collect_behavioral_surface_inventory(intelligence)

    def _translate_batch(batch: Any) -> ModelTranslation:
        return translator.translate(
            language=batch.language,
            intelligence=intelligence,
            skills=ordered_skills,
            egress_policy_id=egress_policy_id,
            root_coordinate=root_coordinate,
            evidence_ids=batch.evidence_ids,
            behavioral_surfaces=tuple(
                surface
                for surface in independent_surfaces.surfaces
                if surface.language == batch.language
                and any(
                    evidence.evidence_id in frozenset(batch.evidence_ids)
                    for evidence in surface.evidence
                )
            ),
            partition_ordinal=batch.language_ordinal,
            partition_count=batch.language_batch_count,
        )

    total_insufficient_evidence_retries = 0
    translations_list: list[ModelTranslation] = []
    for batch in batch_plan.batches:
        attempt = 0
        while True:
            attempt += 1
            translation = _translate_batch(batch)
            if (
                not _translation_has_insufficient_evidence(translation)
                or attempt > _INSUFFICIENT_EVIDENCE_RETRY_ATTEMPTS
            ):
                break
            with log_operation(
                "model_translation_insufficient_evidence_retry",
                language=batch.language,
                attempt=attempt,
                bound=_INSUFFICIENT_EVIDENCE_RETRY_ATTEMPTS,
                call_id=translation.journal.call_id,
            ):
                pass
        total_insufficient_evidence_retries += attempt - 1
        translations_list.append(translation)
    translations = tuple(translations_list)
    guard.require_unchanged()
    definitions = tuple(
        observation
        for translation in translations
        for observation in translation.observations
    )
    translator_identity = canonical_digest(
        {
            "schema": "literate-ai/inverse-translator-set@1",
            "calls": [
                {
                    "call_id": item.journal.call_id,
                    "selection_identity": item.journal.selection_identity,
                    "tool_binding_identity": item.journal.tool_binding_identity,
                }
                for item in translations
            ],
        }
    )
    surface_inventory = _reconcile_model_surfaces(
        independent_surfaces,
        intelligence,
        definitions,
        translator_identity,
    )
    surface_inventory.require_complete()
    component_graph = _merge_component_graphs(
        tuple(item.component_graph for item in translations)
    )
    base_definitions = tuple(
        item.scope == "base"
        and item.claim_kind in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
        for item in definitions
    )
    if not any(base_definitions):
        raise SourceToSpecificationError(
            "model_translation.base_behavior_missing",
            "model translators emitted no evidence-backed base behavior",
        )
    candidate_statements = tuple(
        DraftStatement(
            statement_id=f"statement:{item.observation_id}",
            capability=f"{item.capability} {item.observation_id[-12:]}",
            requirement=item.requirement,
            scenarios=(item.scenario,),
            observation_ids=(item.observation_id,),
        )
        for item, is_base in zip(definitions, base_definitions, strict=True)
        if is_base
    )
    layered_output = benefits_from_literate_markdown(
        component_graph, candidate_statements
    )
    output_provider = "literate-markdown" if layered_output else "openspec"
    evidence_by_id = {
        item.reference.evidence_id: item.reference for item in intelligence.evidence
    }
    evidence_content_by_id = {
        item.reference.evidence_id: item.content for item in intelligence.evidence
    }
    journal_by_observation = {
        observation.observation_id: translation.journal
        for translation in translations
        for observation in translation.observations
    }

    def _literal_asset_for(
        item: ModelObservationDraft,
    ) -> tuple[str, str, str] | None:
        """Return (asset path, JSON payload, digest) if evidence cites literal data.

        Detection is purely syntactic (see `.literal_data`), independent of any
        model output, so a translator cannot make a literal table disappear by
        paraphrasing it into prose instead of citing it.
        """

        for evidence_id in item.evidence_ids:
            reference = evidence_by_id.get(evidence_id)
            content = evidence_content_by_id.get(evidence_id)
            if reference is None or content is None:
                continue
            for table in python_literal_tables(content):
                path = literal_asset_path(source_path=reference.path, name=table.name)
                payload = canonical_literal_table_json(table.value)
                digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
                return path, payload, digest
        return None

    literal_data_artifacts: dict[str, DraftArtifact] = {}
    surfaces = []
    for item in definitions:
        literal_asset = _literal_asset_for(item)
        if item.scope.startswith("flavor:"):
            declared_state = CoverageState.IMPLEMENTATION_DETAIL
            reason = (
                "Language implementation choices remain in a separate Flavor proposal."
            )
        elif literal_asset is not None:
            asset_path, payload, digest = literal_asset
            literal_data_artifacts.setdefault(
                asset_path, DraftArtifact(asset_path, payload)
            )
            declared_state = CoverageState.ASSET_PINNED
            reason = (
                f"Literal constant values are pinned verbatim in asset "
                f"`{asset_path}` ({digest}) instead of being restated in prose."
            )
        else:
            declared_state = None
            reason = ""
        surfaces.append(
            BehaviorSurface(
                surface_id=f"model-surface:{item.observation_id.removeprefix('model-observation:')}",
                facet=item.facet,
                description=f"Evidence-backed {item.language} {item.facet} behavior",
                declared_state=declared_state,
                reason=reason,
            )
        )
    surfaces = tuple(surfaces)
    surface_by_observation = {
        item.observation_id: surface.surface_id
        for item, surface in zip(definitions, surfaces, strict=True)
    }
    request = SourceToSpecificationRequest(
        request_id=canonical_digest(
            {
                "source_snapshot_id": inventory.identity,
                "origin_attestation_id": origin_attestation_id,
                "skill_set_identity": skill_set.identity,
                "intelligence_identity": intelligence.identity,
                "model_call_ids": tuple(item.journal.call_id for item in translations),
                "evidence_batch_plan_identity": batch_plan.identity,
                "component_graph_identity": component_graph.identity,
                "output_provider": output_provider,
                "mode": mode.value,
                "previous_specification_set_id": previous_specification_set_id,
            }
        ),
        source_snapshot_id=inventory.identity,
        source_content_digest=inventory.identity,
        origin_attestation_id=origin_attestation_id,
        mode=mode,
        output_provider=output_provider,
        skill_set_id=skill_set.skill_set_id,
        routing_policy_id="selected-coding-cli-inverse-per-language@1",
        redaction_policy_id="classified-sensitive-content-digest-only@1",
        egress_policy_id=egress_policy_id,
        included_paths=tuple(item.path for item in inventory.entries),
        excluded_paths=inventory.excluded_directories,
        facets=tuple(sorted({item.facet for item in definitions})),
        previous_specification_set_id=previous_specification_set_id,
    )
    definitions_by_skill: dict[str, list[ModelObservationDraft]] = {}
    for definition in definitions:
        definitions_by_skill.setdefault(definition.skill_id, []).append(definition)
    stage_runs: list[dict[str, Any]] = []

    def execute_skill(executing_skill, _request, _surfaces, _evidence):
        selected = tuple(definitions_by_skill.get(executing_skill.skill_id, ()))
        emitted = tuple(
            BehaviorObservation(
                observation_id=item.observation_id,
                surface_ids=(surface_by_observation[item.observation_id],),
                facet=item.facet,
                claim_kind=item.claim_kind,
                statement=item.requirement,
                evidence=tuple(evidence_by_id[value] for value in item.evidence_ids),
                skill=executing_skill.ref,
                confidence=item.confidence_basis_points / 10_000,
                model_call_id=journal_by_observation[item.observation_id].call_id,
            )
            for item in selected
        )
        call_ids = tuple(
            sorted(
                {
                    journal_by_observation[item.observation_id].call_id
                    for item in selected
                }
            )
        )
        stage_runs.append(
            {
                "skill": canonical_value(executing_skill.ref),
                "executor": "source-intelligence-coding-cli@1",
                "observation_ids": [item.observation_id for item in emitted],
                "model_call_ids": list(call_ids),
            }
        )
        return emitted

    def render_draft(_request, observations, uncertainty):
        candidate_by_observation = {
            statement.observation_ids[0]: statement
            for statement in candidate_statements
        }
        statements = tuple(
            candidate_by_observation[item.observation_id]
            for item in observations
            if item.observation_id in candidate_by_observation
        )
        statements_by_observation = {
            statement.observation_ids[0]: statement for statement in statements
        }
        statement_partitions: list[tuple[str, tuple[DraftStatement, ...]]] = []
        for node in component_graph.nodes:
            selected = tuple(
                statements_by_observation[observation_id]
                for observation_id in node.observation_ids
                if observation_id in statements_by_observation
            )
            if not selected:
                raise SourceToSpecificationError(
                    "model_translation.component_specification_empty",
                    f"Component {node.coordinate} has no evidence-backed base behavior",
                )
            statement_partitions.append((node.coordinate, selected))
        if layered_output:
            return statements, (
                *render_literate_markdown_artifacts(
                    component_graph, tuple(statement_partitions), uncertainty
                ),
                *literal_data_artifacts.values(),
            )
        coordinate, selected = statement_partitions[0]
        node = component_graph.nodes[0]
        return statements, (
            DraftArtifact(
                component_specification_path(coordinate, graph_size=1),
                render_component_openspec_markdown(node, selected),
            ),
            *(
                DraftArtifact(
                    capability_contract_path(contract.name),
                    render_capability_contract_markdown(contract),
                )
                for contract in node.capability_contracts
            ),
            *literal_data_artifacts.values(),
        )

    root_node = next(
        item
        for item in component_graph.nodes
        if item.coordinate == component_graph.root_coordinate
    )

    result = SourceToSpecificationWorkflow().run(
        request=request,
        skill_set=skill_set,
        skill_catalog=skill_catalog,
        surfaces=surfaces,
        evidence=tuple(item.reference for item in intelligence.evidence),
        execute_skill=execute_skill,
        render_draft=render_draft,
        validate_provider=(
            validate_literate_markdown_draft
            if layered_output
            else validate_openspec_draft
        ),
        component_definition_draft=ComponentDefinitionDraft(
            coordinate=root_node.coordinate,
            title=root_node.title,
            provided_capabilities=root_node.provided_capabilities,
            source_snapshot_id=inventory.identity,
        ),
        component_graph_draft=component_graph,
        source_root_guard=root,
        trusted_skill_classifications=trusted_skill_classifications,
    )
    return ModelDerivation(
        result=result,
        flavor_drafts=_flavor_drafts(definitions),
        stage_runs=tuple(stage_runs),
        intelligence=intelligence,
        journals=tuple(item.journal for item in translations),
        surface_inventory=surface_inventory,
        evidence_partition_manifest=partition_manifest,
        evidence_batch_plan=batch_plan,
        insufficient_evidence_retry_count=total_insufficient_evidence_retries,
    )


__all__ = [
    "INVERSE_EVIDENCE_CUSTODY_SCHEMA",
    "InverseLanguageTranslator",
    "IntelligenceQueryRecord",
    "LANGUAGE_SKILL_IDS",
    "LEGACY_MODEL_TRANSLATION_MODE",
    "LEGACY_SEMANTIC_SOURCE_TRANSLATION_RUN_SCHEMA",
    "LEGACY_SOURCE_INTELLIGENCE_SCHEMA",
    "LEGACY_SOURCE_TRANSLATION_RUN_SCHEMA",
    "MODEL_CALL_JOURNAL_SCHEMA",
    "MODEL_TRANSLATION_OUTPUT_SCHEMA",
    "MODEL_TRANSLATION_MODE",
    "PREVIOUS_MODEL_CALL_JOURNAL_SCHEMA",
    "PREVIOUS_SOURCE_TRANSLATION_RUN_SCHEMA",
    "ModelCallJournal",
    "ModelDerivation",
    "ModelEvidence",
    "ModelRelationshipEvidence",
    "ModelObservationDraft",
    "ModelTranslation",
    "SOURCE_INTELLIGENCE_SCHEMA",
    "SOURCE_TRANSLATION_RUN_SCHEMA",
    "SUPPORTED_INVERSE_LANGUAGES",
    "SourceIntelligence",
    "SourceIntelligenceCollector",
    "UnresolvedRelationshipEvidence",
    "derive_model_checkout",
    "model_call_journal_from_dict",
    "parse_model_observations",
    "parse_model_component_graph",
    "source_intelligence_from_dict",
    "validate_model_translation_record",
    "validate_inverse_evidence_custody",
    "validate_qualification_inverse_evidence",
]
