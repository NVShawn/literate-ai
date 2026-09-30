"""Immutable, standard-library contracts for inverse specification authoring."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar

from literate_ai.contracts import semantic_version
from literate_ai.contracts.library_imports import (
    AuthoredLibraryImport,
    validate_library_imports,
)

from .errors import SourceToSpecificationError


def _text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SourceToSpecificationError(
            "contract.empty_text", f"{field_name} must not be empty"
        )
    return normalized


def _unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise SourceToSpecificationError(
            "contract.duplicate_value", f"{field_name} must contain unique values"
        )
    return normalized


def _relative_path(value: str, field_name: str) -> str:
    normalized = _text(value, field_name)
    if "\\" in normalized:
        raise SourceToSpecificationError(
            "contract.invalid_path", f"{field_name} must use POSIX separators"
        )
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceToSpecificationError(
            "contract.invalid_path", f"{field_name} must be source-relative"
        )
    return path.as_posix()


def _freeze_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(
        {
            _text(str(key), "extension key"): _text(str(item), "extension value")
            for key, item in sorted(value.items())
        }
    )


def _freeze_models(value: Mapping[str, str]) -> Mapping[str, str]:
    providers = {"codex", "claude", "cursor-agent", "opencode"}
    normalized = _freeze_mapping(value)
    if any(provider not in providers for provider in normalized):
        raise SourceToSpecificationError(
            "skill.model_provider_unsupported",
            "skill models may select only codex, claude, cursor-agent, or opencode",
        )
    if any(selector == "cli-configured-default" for selector in normalized.values()):
        raise SourceToSpecificationError(
            "skill.model_selector_reserved",
            "cli-configured-default represents omission and cannot be selected",
        )
    return normalized


def canonical_value(value: Any) -> Any:
    """Return a JSON-compatible semantic representation for hashing and fixtures."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: canonical_value(getattr(value, item.name))
            for item in dataclasses.fields(value)
            if item.metadata.get("identity", True)
            and not (
                item.metadata.get("omit_empty", False)
                and getattr(value, item.name) == ()
            )
        }
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 identity of canonical semantic JSON."""

    payload = json.dumps(
        canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class RunMode(StrEnum):
    BOOTSTRAP = "bootstrap"
    AUDIT = "audit"
    REFRESH = "refresh"
    CONFORMANCE = "conformance"


class ClaimKind(StrEnum):
    OBSERVED = "observed-current-behavior"
    INFERRED_INTENT = "inferred-intent"
    SUSPECTED_DEFECT = "suspected-defect"
    COMPATIBILITY_QUIRK = "compatibility-quirk"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class CoverageState(StrEnum):
    COVERED = "covered"
    EXCLUDED = "intentionally-excluded"
    IMPLEMENTATION_DETAIL = "implementation-detail"
    ASSET_PINNED = "asset-pinned"
    UNRESOLVED = "unresolved"
    CONFLICTING = "conflicting"
    UNSUPPORTED = "unsupported"


class UncertaintyKind(StrEnum):
    AMBIGUITY = "ambiguity"
    CONFLICT = "conflict"
    SUSPECTED_DEFECT = "suspected-defect"
    MISSING_EVIDENCE = "missing-evidence"
    SKILL_DISAGREEMENT = "skill-disagreement"
    UNKNOWN = "unknown"


class ReviewDisposition(StrEnum):
    ACCEPT = "accept"
    EDIT = "edit"
    REJECT = "reject"
    SUPERSEDE = "supersede"
    DEFER = "defer"


class ReviewAuthority(StrEnum):
    HUMAN = "human"
    POLICY = "authorized-policy"


@dataclass(frozen=True, slots=True)
class SkillRef:
    skill_id: str
    version: str
    content_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_id", _text(self.skill_id, "skill_id"))
        object.__setattr__(
            self, "version", semantic_version(self.version, "SkillRef.version")
        )
        object.__setattr__(
            self, "content_digest", _text(self.content_digest, "content_digest")
        )


@dataclass(frozen=True, slots=True)
class SpecAuthoringSkill:
    schema: ClassVar[str] = "literate-ai/spec-authoring-skill@1"

    skill_id: str
    version: str
    content_digest: str
    title: str
    capabilities: tuple[str, ...]
    facets: tuple[str, ...]
    evidence_kinds: tuple[str, ...]
    model_capabilities: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    trust_classification: str = "builtin-reviewed"
    prompt_template: str = ""
    extensions: Mapping[str, str] = field(default_factory=dict)
    models: Mapping[str, str] = field(default_factory=dict)
    dependency_refs: tuple[SkillRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_id", _text(self.skill_id, "skill_id"))
        object.__setattr__(
            self,
            "version",
            semantic_version(self.version, "SpecAuthoringSkill.version"),
        )
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(
            self, "content_digest", _text(self.content_digest, "content_digest")
        )
        for name in (
            "capabilities",
            "facets",
            "evidence_kinds",
            "model_capabilities",
            "dependencies",
            "after",
            "limitations",
        ):
            object.__setattr__(self, name, _unique(getattr(self, name), name))
        if not self.capabilities or not self.facets or not self.evidence_kinds:
            raise SourceToSpecificationError(
                "skill.incomplete",
                "skills require capabilities, facets, and evidence kinds",
            )
        object.__setattr__(
            self,
            "trust_classification",
            _text(self.trust_classification, "trust_classification"),
        )
        object.__setattr__(self, "prompt_template", self.prompt_template.strip())
        object.__setattr__(self, "extensions", _freeze_mapping(self.extensions))
        object.__setattr__(self, "models", _freeze_models(self.models))
        dependency_ids = tuple(item.skill_id for item in self.dependency_refs)
        if len(dependency_ids) != len(set(dependency_ids)):
            raise SourceToSpecificationError(
                "skill.duplicate_dependency_ref",
                "exact skill dependency refs must have unique skill IDs",
            )
        if self.dependency_refs and self.dependencies:
            if set(dependency_ids) != set(self.dependencies):
                raise SourceToSpecificationError(
                    "skill.dependency_ref_mismatch",
                    "exact dependency refs must match legacy dependency IDs",
                )

    @property
    def ref(self) -> SkillRef:
        return SkillRef(self.skill_id, self.version, self.content_digest)

    def model_for(self, coding_cli: str) -> str | None:
        return self.models.get(coding_cli)


@dataclass(frozen=True, slots=True)
class SpecAuthoringSkillSet:
    schema: ClassVar[str] = "literate-ai/spec-authoring-skill-set@1"

    skill_set_id: str
    version: str
    skills: tuple[SkillRef, ...]
    conflict_policy: str = "retain"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "skill_set_id", _text(self.skill_set_id, "skill_set_id")
        )
        object.__setattr__(
            self,
            "version",
            semantic_version(self.version, "SpecAuthoringSkillSet.version"),
        )
        object.__setattr__(
            self, "conflict_policy", _text(self.conflict_policy, "conflict_policy")
        )
        if not self.skills:
            raise SourceToSpecificationError(
                "skill_set.empty", "a skill set requires at least one skill"
            )
        ids = tuple(item.skill_id for item in self.skills)
        if len(ids) != len(set(ids)):
            raise SourceToSpecificationError(
                "skill_set.duplicate", "a skill set cannot repeat a skill"
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class SourceToSpecificationRequest:
    schema: ClassVar[str] = "literate-ai/source-to-specification-request@1"

    request_id: str
    source_snapshot_id: str
    source_content_digest: str
    origin_attestation_id: str
    mode: RunMode
    output_provider: str
    skill_set_id: str
    routing_policy_id: str
    redaction_policy_id: str
    egress_policy_id: str
    included_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    facets: tuple[str, ...] = ()
    previous_specification_set_id: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "mode", RunMode(self.mode))
        except ValueError as exc:
            raise SourceToSpecificationError(
                "request.invalid_mode", f"unsupported run mode: {self.mode!r}"
            ) from exc
        for name in (
            "request_id",
            "source_snapshot_id",
            "source_content_digest",
            "origin_attestation_id",
            "output_provider",
            "skill_set_id",
            "routing_policy_id",
            "redaction_policy_id",
            "egress_policy_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "included_paths",
            tuple(
                _relative_path(item, "included_paths") for item in self.included_paths
            ),
        )
        object.__setattr__(
            self,
            "excluded_paths",
            tuple(
                _relative_path(item, "excluded_paths") for item in self.excluded_paths
            ),
        )
        object.__setattr__(self, "facets", _unique(self.facets, "facets"))
        if (
            self.mode is not RunMode.BOOTSTRAP
            and not self.previous_specification_set_id
        ):
            raise SourceToSpecificationError(
                "request.previous_specification_required",
                f"{self.mode.value} mode requires a previous specification set",
            )
        if self.previous_specification_set_id is not None:
            object.__setattr__(
                self,
                "previous_specification_set_id",
                _text(
                    self.previous_specification_set_id,
                    "previous_specification_set_id",
                ),
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    source_snapshot_id: str
    content_digest: str
    path: str
    symbol: str = ""

    def __post_init__(self) -> None:
        for name in ("evidence_id", "source_snapshot_id", "content_digest"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "path", _relative_path(self.path, "path"))
        object.__setattr__(self, "symbol", self.symbol.strip())


@dataclass(frozen=True, slots=True)
class BehaviorSurface:
    surface_id: str
    facet: str
    description: str
    declared_state: CoverageState | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        for name in ("surface_id", "facet", "description"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.declared_state not in {
            None,
            CoverageState.EXCLUDED,
            CoverageState.IMPLEMENTATION_DETAIL,
            CoverageState.ASSET_PINNED,
        }:
            raise SourceToSpecificationError(
                "surface.invalid_declared_state",
                "only excluded, implementation-detail, and asset-pinned may be "
                "predeclared",
            )
        object.__setattr__(self, "reason", self.reason.strip())
        if self.declared_state is not None and not self.reason:
            raise SourceToSpecificationError(
                "surface.reason_required",
                "predeclared coverage state requires a reason",
            )


@dataclass(frozen=True, slots=True)
class BehaviorObservation:
    observation_id: str
    surface_ids: tuple[str, ...]
    facet: str
    claim_kind: ClaimKind
    statement: str
    evidence: tuple[EvidenceReference, ...]
    skill: SkillRef
    confidence: float
    model_call_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("observation_id", "facet", "statement"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self, "surface_ids", _unique(self.surface_ids, "surface_ids")
        )
        if not self.surface_ids:
            raise SourceToSpecificationError(
                "observation.surface_required", "an observation requires a surface"
            )
        if not self.evidence and self.claim_kind is not ClaimKind.UNKNOWN:
            raise SourceToSpecificationError(
                "observation.evidence_required",
                "non-unknown observations require exact evidence",
            )
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise SourceToSpecificationError(
                "observation.invalid_confidence",
                "confidence must be between zero and one",
            )
        if self.model_call_id is not None:
            object.__setattr__(
                self, "model_call_id", _text(self.model_call_id, "model_call_id")
            )


@dataclass(frozen=True, slots=True)
class DraftScenario:
    name: str
    when: str
    then: str

    def __post_init__(self) -> None:
        for name in ("name", "when", "then"):
            object.__setattr__(self, name, _text(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class DraftStatement:
    statement_id: str
    capability: str
    requirement: str
    scenarios: tuple[DraftScenario, ...]
    observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("statement_id", "capability", "requirement"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not self.scenarios:
            raise SourceToSpecificationError(
                "draft.scenario_required", "a draft statement requires a scenario"
            )
        object.__setattr__(
            self,
            "observation_ids",
            _unique(self.observation_ids, "observation_ids"),
        )
        if not self.observation_ids:
            raise SourceToSpecificationError(
                "draft.observation_required",
                "a normative draft statement requires observations",
            )


@dataclass(frozen=True, slots=True)
class DraftArtifact:
    path: str
    content: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path, "path"))
        if not self.content.strip():
            raise SourceToSpecificationError(
                "draft.empty_artifact", "a draft artifact must not be empty"
            )

    @property
    def content_digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.content.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ProviderValidation:
    provider: str
    provider_version: str
    valid: bool
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _text(self.provider, "provider"))
        object.__setattr__(
            self,
            "provider_version",
            _text(self.provider_version, "provider_version"),
        )
        object.__setattr__(
            self, "diagnostics", tuple(item.strip() for item in self.diagnostics)
        )
        if self.valid and any(self.diagnostics):
            raise SourceToSpecificationError(
                "validation.valid_with_diagnostics",
                "valid provider results cannot contain error diagnostics",
            )


@dataclass(frozen=True, slots=True)
class SpecificationDraftSet:
    schema: ClassVar[str] = "literate-ai/specification-draft-set@1"

    draft_id: str
    request_id: str
    source_snapshot_id: str
    output_provider: str
    skill_set_identity: str
    statements: tuple[DraftStatement, ...]
    artifacts: tuple[DraftArtifact, ...]
    validation: ProviderValidation

    def __post_init__(self) -> None:
        for name in (
            "draft_id",
            "request_id",
            "source_snapshot_id",
            "output_provider",
            "skill_set_identity",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for values, code in (
            (tuple(item.statement_id for item in self.statements), "statement"),
            (tuple(item.path for item in self.artifacts), "artifact"),
        ):
            if len(values) != len(set(values)):
                raise SourceToSpecificationError(
                    f"draft.duplicate_{code}", f"draft {code}s must be unique"
                )
        if not self.artifacts:
            raise SourceToSpecificationError(
                "draft.artifact_required", "a draft set requires provider artifacts"
            )

    @property
    def identity(self) -> str:
        return self.draft_id


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    surface_id: str
    state: CoverageState
    observation_ids: tuple[str, ...] = ()
    statement_ids: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface_id", _text(self.surface_id, "surface_id"))
        object.__setattr__(
            self, "observation_ids", _unique(self.observation_ids, "observation_ids")
        )
        object.__setattr__(
            self, "statement_ids", _unique(self.statement_ids, "statement_ids")
        )
        object.__setattr__(self, "reason", self.reason.strip())
        if self.state is not CoverageState.COVERED and not self.reason:
            raise SourceToSpecificationError(
                "coverage.reason_required", "non-covered states require a reason"
            )


@dataclass(frozen=True, slots=True)
class SourceSpecificationCoverageMap:
    request_id: str
    entries: tuple[CoverageEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id, "request_id"))
        ids = tuple(item.surface_id for item in self.entries)
        if len(ids) != len(set(ids)):
            raise SourceToSpecificationError(
                "coverage.duplicate_surface", "coverage may describe each surface once"
            )


@dataclass(frozen=True, slots=True)
class UncertaintyItem:
    uncertainty_id: str
    kind: UncertaintyKind
    message: str
    observation_ids: tuple[str, ...] = ()
    surface_ids: tuple[str, ...] = ()
    blocking: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "uncertainty_id", _text(self.uncertainty_id, "uncertainty_id")
        )
        object.__setattr__(self, "message", _text(self.message, "message"))
        object.__setattr__(
            self, "observation_ids", _unique(self.observation_ids, "observation_ids")
        )
        object.__setattr__(
            self, "surface_ids", _unique(self.surface_ids, "surface_ids")
        )


@dataclass(frozen=True, slots=True)
class UncertaintyLedger:
    request_id: str
    items: tuple[UncertaintyItem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id, "request_id"))
        ids = tuple(item.uncertainty_id for item in self.items)
        if len(ids) != len(set(ids)):
            raise SourceToSpecificationError(
                "uncertainty.duplicate", "uncertainty IDs must be unique"
            )


@dataclass(frozen=True, slots=True)
class ComponentDefinitionDraft:
    coordinate: str
    title: str
    provided_capabilities: tuple[str, ...]
    source_snapshot_id: str

    def __post_init__(self) -> None:
        for name in ("coordinate", "title", "source_snapshot_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "provided_capabilities",
            _unique(self.provided_capabilities, "provided_capabilities"),
        )


@dataclass(frozen=True, slots=True)
class ComponentCapabilityContractDraft:
    """Reviewed public meaning of one capability recovered from source evidence."""

    name: str
    contract: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("name", "contract"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self, "evidence_ids", _unique(self.evidence_ids, "evidence_ids")
        )
        if not self.evidence_ids:
            raise SourceToSpecificationError(
                "component_graph.capability_evidence_required",
                "each capability contract requires exact source evidence",
            )


@dataclass(frozen=True, slots=True)
class ComponentEntrypointDraft:
    """Reviewed executable boundary; libraries deliberately have none."""

    name: str
    kind: str
    path: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("name", "kind"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "path", _relative_path(self.path, "path"))
        object.__setattr__(
            self, "evidence_ids", _unique(self.evidence_ids, "evidence_ids")
        )
        if not self.evidence_ids:
            raise SourceToSpecificationError(
                "component_graph.entrypoint_evidence_required",
                "each entrypoint requires exact source evidence",
            )


@dataclass(frozen=True, slots=True)
class ComponentGraphNodeDraft:
    """One evidence-backed logical Component recovered from a source tree."""

    coordinate: str
    title: str
    kind: str
    profiles: tuple[str, ...]
    provided_capabilities: tuple[str, ...]
    capability_contracts: tuple[ComponentCapabilityContractDraft, ...]
    entrypoints: tuple[ComponentEntrypointDraft, ...]
    build_needs: tuple[str, ...]
    source_paths: tuple[str, ...]
    observation_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    library_imports: tuple[AuthoredLibraryImport, ...] = field(
        default=(), metadata={"omit_empty": True}
    )

    def __post_init__(self) -> None:
        for name in ("coordinate", "title"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.kind not in {"unknown", "library", "cli", "service"}:
            raise SourceToSpecificationError(
                "component_graph.kind_unknown",
                "Component kind must be exactly library, cli, or service",
            )
        object.__setattr__(self, "profiles", _unique(self.profiles, "profiles"))
        object.__setattr__(
            self, "build_needs", _unique(self.build_needs, "build_needs")
        )
        if self.kind == "unknown":
            if any(
                (
                    self.profiles,
                    self.capability_contracts,
                    self.entrypoints,
                    self.build_needs,
                )
            ):
                raise SourceToSpecificationError(
                    "component_graph.legacy_semantics_invalid",
                    "unknown legacy Component semantics must not contain invented "
                    "values",
                )
        elif not self.profiles or not self.build_needs:
            raise SourceToSpecificationError(
                "component_graph.semantics_incomplete",
                "each Component requires reviewed profiles and build needs",
            )
        object.__setattr__(
            self,
            "provided_capabilities",
            _unique(self.provided_capabilities, "provided_capabilities"),
        )
        try:
            validate_library_imports(
                self.library_imports,
                kind=self.kind,
                capabilities=self.provided_capabilities,
            )
        except (TypeError, ValueError) as exc:
            raise SourceToSpecificationError(
                "component_graph.library_imports_invalid",
                "native import declarations must cover reviewed library capabilities",
            ) from exc
        if self.kind != "unknown" and (
            any(
                not isinstance(item, ComponentCapabilityContractDraft)
                for item in self.capability_contracts
            )
            or tuple(item.name for item in self.capability_contracts)
            != tuple(sorted(self.provided_capabilities))
        ):
            raise SourceToSpecificationError(
                "component_graph.capability_contract_mismatch",
                "capability contracts must cover every provided capability in "
                "canonical order",
            )
        if any(
            not isinstance(item, ComponentEntrypointDraft) for item in self.entrypoints
        ):
            raise SourceToSpecificationError(
                "component_graph.entrypoint_invalid",
                "Component entrypoints must be reviewed entrypoint drafts",
            )
        entrypoint_names = tuple(item.name for item in self.entrypoints)
        if entrypoint_names != tuple(sorted(entrypoint_names)) or len(
            entrypoint_names
        ) != len(set(entrypoint_names)):
            raise SourceToSpecificationError(
                "component_graph.entrypoint_invalid",
                "Component entrypoints must have unique names in canonical order",
            )
        if self.kind == "library" and self.entrypoints:
            raise SourceToSpecificationError(
                "component_graph.library_entrypoint_invalid",
                "library Components must not invent runnable entrypoints",
            )
        if self.kind in {"cli", "service"} and not self.entrypoints:
            raise SourceToSpecificationError(
                "component_graph.entrypoint_unknown",
                "CLI and service Components require at least one reviewed entrypoint",
            )
        object.__setattr__(
            self,
            "source_paths",
            _unique(
                tuple(
                    _relative_path(item, "source_paths") for item in self.source_paths
                ),
                "source_paths",
            ),
        )
        object.__setattr__(
            self,
            "observation_ids",
            _unique(self.observation_ids, "observation_ids"),
        )
        object.__setattr__(
            self,
            "evidence_ids",
            _unique(self.evidence_ids, "evidence_ids"),
        )
        if not all(
            (
                self.provided_capabilities,
                self.source_paths,
                self.observation_ids,
                self.evidence_ids,
            )
        ):
            raise SourceToSpecificationError(
                "component_graph.node_incomplete",
                "each Component graph node requires capabilities, source paths, "
                "observations, and evidence",
            )


@dataclass(frozen=True, slots=True)
class ComponentGraphEdgeDraft:
    """One reviewed capability dependency between recovered Components."""

    source_coordinate: str
    target_coordinate: str
    requirement_id: str
    capability: str
    version_range: str
    dependency_kind: str
    optional: bool
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "source_coordinate",
            "target_coordinate",
            "requirement_id",
            "capability",
            "version_range",
            "dependency_kind",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.source_coordinate == self.target_coordinate:
            raise SourceToSpecificationError(
                "component_graph.self_dependency",
                "a Component graph edge cannot depend on itself",
            )
        if self.dependency_kind not in {
            "build",
            "documentation",
            "generation",
            "runtime",
            "validation",
        }:
            raise SourceToSpecificationError(
                "component_graph.dependency_kind_invalid",
                "Component graph dependency kind is unsupported",
            )
        if not isinstance(self.optional, bool):
            raise SourceToSpecificationError(
                "component_graph.optional_invalid",
                "Component graph optionality must be boolean",
            )
        object.__setattr__(
            self,
            "evidence_ids",
            _unique(self.evidence_ids, "evidence_ids"),
        )
        if not self.evidence_ids:
            raise SourceToSpecificationError(
                "component_graph.edge_evidence_required",
                "Component graph edges require exact source evidence",
            )


@dataclass(frozen=True, slots=True)
class ComponentGraphDraft:
    """A rooted, acyclic Component graph proposed from exact source evidence."""

    source_snapshot_id: str
    root_coordinate: str
    nodes: tuple[ComponentGraphNodeDraft, ...]
    edges: tuple[ComponentGraphEdgeDraft, ...]

    def __post_init__(self) -> None:
        for name in ("source_snapshot_id", "root_coordinate"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        nodes = {item.coordinate: item for item in self.nodes}
        if not self.nodes or len(nodes) != len(self.nodes):
            raise SourceToSpecificationError(
                "component_graph.node_invalid",
                "Component graph nodes must be non-empty and have unique coordinates",
            )
        if self.root_coordinate not in nodes:
            raise SourceToSpecificationError(
                "component_graph.root_missing",
                "Component graph root must name one recovered node",
            )
        observation_ids = tuple(
            observation_id
            for node in self.nodes
            for observation_id in node.observation_ids
        )
        if len(observation_ids) != len(set(observation_ids)):
            raise SourceToSpecificationError(
                "component_graph.observation_duplicate",
                "each observation must belong to exactly one Component graph node",
            )
        specification_paths = tuple(
            re.sub(r"[^a-zA-Z0-9_.-]+", "-", item.coordinate).strip("-.")
            for item in self.nodes
        )
        if any(not item for item in specification_paths) or len(
            specification_paths
        ) != len(set(specification_paths)):
            raise SourceToSpecificationError(
                "component_graph.coordinate_path_collision",
                "Component coordinates must map to unique portable specification paths",
            )
        edge_keys = tuple(
            (item.source_coordinate, item.requirement_id) for item in self.edges
        )
        if len(edge_keys) != len(set(edge_keys)):
            raise SourceToSpecificationError(
                "component_graph.requirement_duplicate",
                "each Component requirement ID must be unique within its source node",
            )
        for edge in self.edges:
            if (
                edge.source_coordinate not in nodes
                or edge.target_coordinate not in nodes
            ):
                raise SourceToSpecificationError(
                    "component_graph.edge_node_unknown",
                    "Component graph edge references an unknown node",
                )
            if (
                edge.capability
                not in nodes[edge.target_coordinate].provided_capabilities
            ):
                raise SourceToSpecificationError(
                    "component_graph.capability_unprovided",
                    "Component graph target does not provide the required capability",
                )

        outgoing: dict[str, tuple[str, ...]] = {
            coordinate: tuple(
                edge.target_coordinate
                for edge in self.edges
                if edge.source_coordinate == coordinate
            )
            for coordinate in nodes
        }
        visited: set[str] = set()
        active: set[str] = set()

        def visit(coordinate: str) -> None:
            if coordinate in active:
                raise SourceToSpecificationError(
                    "component_graph.cycle",
                    "recovered Component graph must be acyclic",
                )
            if coordinate in visited:
                return
            active.add(coordinate)
            for target in outgoing[coordinate]:
                visit(target)
            active.remove(coordinate)
            visited.add(coordinate)

        visit(self.root_coordinate)
        if visited != set(nodes):
            raise SourceToSpecificationError(
                "component_graph.unreachable_node",
                "every recovered Component must be reachable from the root",
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class SourceToSpecificationRun:
    run_id: str
    request_identity: str
    skill_set_identity: str
    observation_ids: tuple[str, ...]
    draft_identity: str

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "request_identity",
            "skill_set_identity",
            "draft_identity",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self, "observation_ids", _unique(self.observation_ids, "observation_ids")
        )


@dataclass(frozen=True, slots=True)
class SourceToSpecificationResult:
    request: SourceToSpecificationRequest
    observations: tuple[BehaviorObservation, ...]
    draft: SpecificationDraftSet
    coverage: SourceSpecificationCoverageMap
    uncertainty: UncertaintyLedger
    run: SourceToSpecificationRun
    component_definition_draft: ComponentDefinitionDraft | None = None
    component_graph_draft: ComponentGraphDraft | None = None


__all__ = [
    "BehaviorObservation",
    "BehaviorSurface",
    "ClaimKind",
    "ComponentCapabilityContractDraft",
    "ComponentDefinitionDraft",
    "ComponentEntrypointDraft",
    "ComponentGraphDraft",
    "ComponentGraphEdgeDraft",
    "ComponentGraphNodeDraft",
    "CoverageEntry",
    "CoverageState",
    "DraftArtifact",
    "DraftScenario",
    "DraftStatement",
    "EvidenceReference",
    "ProviderValidation",
    "ReviewAuthority",
    "ReviewDisposition",
    "RunMode",
    "SkillRef",
    "SourceSpecificationCoverageMap",
    "SourceToSpecificationRequest",
    "SourceToSpecificationResult",
    "SourceToSpecificationRun",
    "SpecAuthoringSkill",
    "SpecAuthoringSkillSet",
    "SpecificationDraftSet",
    "UncertaintyItem",
    "UncertaintyKind",
    "UncertaintyLedger",
    "canonical_digest",
    "canonical_value",
]
