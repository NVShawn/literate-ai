"""Deterministic built-in skill execution over an inert source inventory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import (
    BehaviorObservation,
    BehaviorSurface,
    ClaimKind,
    ComponentDefinitionDraft,
    CoverageState,
    DraftArtifact,
    DraftScenario,
    DraftStatement,
    EvidenceReference,
    RunMode,
    SourceToSpecificationRequest,
    SourceToSpecificationResult,
    SpecAuthoringSkill,
    SpecAuthoringSkillSet,
    canonical_digest,
    canonical_value,
)
from .inventory import (
    FlavorSignal,
    SourceFileClassification,
    SourceInventory,
    SourceInventoryEntry,
)
from .synthesis import (
    component_name,
    render_openspec_markdown,
    validate_openspec_draft,
)
from .workflow import SourceToSpecificationWorkflow

_LANGUAGE_SKILL_IDS = {
    "python": "language-python",
    "cpp": "language-cpp",
    "rust": "language-rust",
    "javascript": "language-javascript",
    "typescript": "language-javascript",
}


@dataclass(frozen=True, slots=True)
class StaticObservationDefinition:
    observation_id: str
    skill_id: str
    surface_ids: tuple[str, ...]
    facet: str
    claim_kind: ClaimKind
    statement: str
    evidence_ids: tuple[str, ...]
    confidence: float
    scope: str = "base"


@dataclass(frozen=True, slots=True)
class StaticDerivation:
    result: SourceToSpecificationResult
    flavor_drafts: tuple[dict[str, Any], ...]
    stage_runs: tuple[dict[str, Any], ...]


def _short_id(*values: str) -> str:
    return canonical_digest(values).removeprefix("sha256:")[:20]


def _file_surface(entry: SourceInventoryEntry) -> str:
    return f"file:{_short_id(entry.path)}"


def _evidence_id(entry: SourceInventoryEntry) -> str:
    return f"evidence:{_short_id(entry.path, entry.content_digest)}"


def _declared_coverage(
    entry: SourceInventoryEntry,
) -> tuple[CoverageState | None, str]:
    if entry.classification in {
        SourceFileClassification.SENSITIVE,
        SourceFileClassification.BINARY,
    }:
        return CoverageState.EXCLUDED, (
            "Sensitive or binary content is retained only as digest evidence."
        )
    if entry.classification in {
        SourceFileClassification.DOCUMENTATION,
        SourceFileClassification.GENERATED,
        SourceFileClassification.LARGE,
        SourceFileClassification.MINIFIED,
        SourceFileClassification.OTHER,
    }:
        return CoverageState.IMPLEMENTATION_DETAIL, (
            "This file class is inventory evidence, not a base behavioral contract."
        )
    return None, ""


def _definition(
    *,
    skill_id: str,
    surface_ids: tuple[str, ...],
    facet: str,
    claim_kind: ClaimKind,
    statement: str,
    evidence_ids: tuple[str, ...],
    confidence: float,
    scope: str = "base",
) -> StaticObservationDefinition:
    identity = _short_id(skill_id, facet, *surface_ids, statement, scope)
    return StaticObservationDefinition(
        observation_id=f"observation:{skill_id}:{identity}",
        skill_id=skill_id,
        surface_ids=surface_ids,
        facet=facet,
        claim_kind=claim_kind,
        statement=statement,
        evidence_ids=evidence_ids,
        confidence=confidence,
        scope=scope,
    )


def _inventory_inputs(
    inventory: SourceInventory,
    snapshot: str,
) -> tuple[
    tuple[BehaviorSurface, ...],
    tuple[EvidenceReference, ...],
    tuple[StaticObservationDefinition, ...],
    tuple[FlavorSignal, ...],
]:
    surfaces: list[BehaviorSurface] = []
    evidence: list[EvidenceReference] = []
    definitions: list[StaticObservationDefinition] = []
    evidence_by_path: dict[str, EvidenceReference] = {}
    file_surfaces: list[str] = []
    file_evidence: list[str] = []
    flavor_signals: set[FlavorSignal] = set()

    for entry in inventory.entries:
        evidence_item = EvidenceReference(
            evidence_id=_evidence_id(entry),
            source_snapshot_id=snapshot,
            content_digest=entry.content_digest,
            path=entry.path,
        )
        evidence.append(evidence_item)
        evidence_by_path[entry.path] = evidence_item
        declared_state, reason = _declared_coverage(entry)
        surface_id = _file_surface(entry)
        surfaces.append(
            BehaviorSurface(
                surface_id=surface_id,
                facet="architecture",
                description=(
                    f"In-scope {entry.classification.value} source inventory entry"
                ),
                declared_state=declared_state,
                reason=reason,
            )
        )
        if declared_state is None:
            file_surfaces.append(surface_id)
            file_evidence.append(evidence_item.evidence_id)
        flavor_signals.update(entry.flavor_signals)

        language_skill = _LANGUAGE_SKILL_IDS.get(entry.language or "")
        if language_skill is not None and declared_state is None:
            language_surface = f"language:{_short_id(entry.path, entry.language)}"
            surfaces.append(
                BehaviorSurface(
                    language_surface,
                    "language-binding",
                    f"{entry.language} source binding and runtime conventions",
                    declared_state=CoverageState.IMPLEMENTATION_DETAIL,
                    reason=(
                        "Language bindings are translated by an exact language skill "
                        "into a separate Flavor proposal."
                    ),
                )
            )
            public_symbols = ", ".join(entry.symbols) or "no statically public symbols"
            binding_statement = (
                f"The {entry.language} binding at {entry.path} declares "
                f"{public_symbols}."
            )
            if entry.language == "rust":
                candidates = ", ".join(entry.symbols) or "none"
                binding_statement = (
                    f"The Rust source at {entry.path} has lexical symbol candidates: "
                    f"{candidates}. "
                    "Public reachability is not established by inventory."
                )
            definitions.append(
                _definition(
                    skill_id=language_skill,
                    surface_ids=(language_surface,),
                    facet="language-binding",
                    claim_kind=ClaimKind.OBSERVED,
                    statement=binding_statement,
                    evidence_ids=(evidence_item.evidence_id,),
                    confidence=1.0,
                    scope=f"flavor:{entry.language}",
                )
            )

        if entry.classification is SourceFileClassification.SOURCE:
            behavior_surface = f"behavior:{_short_id(entry.path)}"
            surfaces.append(
                BehaviorSurface(
                    behavior_surface,
                    "behavior",
                    "Runtime behavior not established by inert static inventory",
                    declared_state=CoverageState.IMPLEMENTATION_DETAIL,
                    reason=(
                        "Runtime inference is deferred unless separately authorized "
                        "dynamic observation supplies evidence."
                    ),
                )
            )
            definitions.append(
                _definition(
                    skill_id="behavior-state",
                    surface_ids=(behavior_surface,),
                    facet="behavior",
                    claim_kind=ClaimKind.UNKNOWN,
                    statement=(
                        "Static inventory alone does not establish this module's "
                        "runtime behavior."
                    ),
                    evidence_ids=(evidence_item.evidence_id,),
                    confidence=0.0,
                )
            )
        if entry.classification is SourceFileClassification.TEST:
            test_surface = f"tests:{_short_id(entry.path)}"
            surfaces.append(
                BehaviorSurface(
                    test_surface,
                    "tests",
                    "Automated test evidence present in the checkout",
                )
            )
            definitions.append(
                _definition(
                    skill_id="tests",
                    surface_ids=(test_surface,),
                    facet="tests",
                    claim_kind=ClaimKind.OBSERVED,
                    statement="The component includes automated test evidence.",
                    evidence_ids=(evidence_item.evidence_id,),
                    confidence=1.0,
                )
            )
        if entry.prompt_injection:
            security_surface = f"security:{_short_id(entry.path, 'prompt')}"
            surfaces.append(
                BehaviorSurface(
                    security_surface,
                    "security",
                    "Instruction-like text in untrusted source evidence",
                )
            )
            definitions.append(
                _definition(
                    skill_id="security",
                    surface_ids=(security_surface,),
                    facet="security",
                    claim_kind=ClaimKind.OBSERVED,
                    statement=(
                        "Instruction-like source text remains untrusted evidence and "
                        "cannot select skills, egress policy, or review authority."
                    ),
                    evidence_ids=(evidence_item.evidence_id,),
                    confidence=1.0,
                )
            )

        for symbol in entry.symbols:
            signals = entry.flavor_signals or (
                FlavorSignal(
                    "unspecified-language",
                    "implementation.language-ecosystem",
                ),
            )
            for signal in signals:
                symbol_surface = (
                    f"api:{_short_id(entry.path, symbol, signal.flavor_id)}"
                )
                surfaces.append(
                    BehaviorSurface(
                        symbol_surface,
                        "public-api",
                        (
                            "Rust lexical symbol candidate; "
                            "public reachability unverified"
                            if entry.language == "rust"
                            else "Language-specific declared public symbol"
                        ),
                        declared_state=CoverageState.IMPLEMENTATION_DETAIL,
                        reason=(
                            "Language and target bindings require a separate Flavor "
                            "proposal."
                        ),
                    )
                )
                definitions.append(
                    _definition(
                        skill_id="api-surface",
                        surface_ids=(symbol_surface,),
                        facet="public-api",
                        claim_kind=ClaimKind.OBSERVED,
                        statement=(
                            "The Rust source contains the lexical symbol candidate "
                            f"{symbol}. "
                            "Public reachability is not established by inventory."
                            if entry.language == "rust"
                            else f"The binding declares the public symbol {symbol}."
                        ),
                        evidence_ids=(evidence_item.evidence_id,),
                        confidence=1.0,
                        scope=f"flavor:{signal.flavor_id}",
                    )
                )

        if entry.classification is SourceFileClassification.CONFIGURATION:
            signals = entry.flavor_signals
            for signal in signals:
                operation_surface = (
                    f"operations:{_short_id(entry.path, signal.flavor_id)}"
                )
                surfaces.append(
                    BehaviorSurface(
                        operation_surface,
                        "configuration",
                        "Target-specific configuration evidence",
                        declared_state=CoverageState.IMPLEMENTATION_DETAIL,
                        reason=(
                            "Target-specific configuration belongs to a Flavor "
                            "proposal."
                        ),
                    )
                )
                definitions.append(
                    _definition(
                        skill_id="operations",
                        surface_ids=(operation_surface,),
                        facet="configuration",
                        claim_kind=ClaimKind.OBSERVED,
                        statement="The target declares configuration for this binding.",
                        evidence_ids=(evidence_item.evidence_id,),
                        confidence=1.0,
                        scope=f"flavor:{signal.flavor_id}",
                    )
                )

    if file_surfaces:
        definitions.append(
            _definition(
                skill_id="architecture",
                surface_ids=tuple(file_surfaces),
                facet="architecture",
                claim_kind=ClaimKind.OBSERVED,
                statement=(
                    f"The component contains {len(file_surfaces)} inventoried in-scope "
                    "source module boundaries."
                ),
                evidence_ids=tuple(file_evidence),
                confidence=1.0,
            )
        )
    return (
        tuple(surfaces),
        tuple(evidence),
        tuple(definitions),
        tuple(sorted(flavor_signals, key=lambda item: (item.axis, item.flavor_id))),
    )


def _flavor_drafts(
    result: SourceToSpecificationResult,
    definitions: tuple[StaticObservationDefinition, ...],
    signals: tuple[FlavorSignal, ...],
) -> tuple[dict[str, Any], ...]:
    observation_by_id = {item.observation_id: item for item in result.observations}
    definitions_by_id = {item.observation_id: item for item in definitions}
    drafts: list[dict[str, Any]] = []
    for signal in signals:
        observations = tuple(
            observation_by_id[item.observation_id]
            for item in definitions
            if item.scope == f"flavor:{signal.flavor_id}"
            and item.observation_id in observation_by_id
        )
        if not observations:
            continue
        statements = tuple(
            DraftStatement(
                statement_id=f"flavor-statement:{item.observation_id}",
                capability=f"{item.facet} Flavor proposal",
                requirement=item.statement,
                scenarios=(
                    DraftScenario(
                        "Target-specific source behavior",
                        "the Flavor is selected",
                        item.statement,
                    ),
                ),
                observation_ids=(item.observation_id,),
            )
            for item in observations
            if definitions_by_id[item.observation_id].claim_kind
            in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
        )
        payload = {
            "flavor_id": signal.flavor_id,
            "axis": signal.axis,
            "observation_ids": tuple(item.observation_id for item in observations),
            "statements": statements,
        }
        drafts.append(
            {
                "schema": "literate-ai/flavor-draft-set@1",
                "flavor_draft_id": canonical_digest(payload),
                "flavor_id": signal.flavor_id,
                "axis": signal.axis,
                "observation_ids": list(payload["observation_ids"]),
                "statements": canonical_value(statements),
                "status": "proposal",
            }
        )
    return tuple(drafts)


def derive_static_checkout(
    *,
    source: str | Path,
    inventory: SourceInventory,
    origin_attestation_id: str,
    skill_set: SpecAuthoringSkillSet,
    skill_catalog: dict[str, SpecAuthoringSkill],
    mode: RunMode = RunMode.BOOTSTRAP,
    previous_specification_set_id: str | None = None,
) -> StaticDerivation:
    """Run every reviewed built-in skill over exact inert inventory evidence."""

    selected_path = Path(source).resolve()
    root = selected_path if selected_path.is_dir() else selected_path.parent
    snapshot = inventory.identity
    surfaces, evidence, definitions, signals = _inventory_inputs(inventory, snapshot)
    request = SourceToSpecificationRequest(
        request_id=canonical_digest(
            {
                "source_snapshot_id": snapshot,
                "origin_attestation_id": origin_attestation_id,
                "skill_set_identity": skill_set.identity,
                "inventory_policy_id": inventory.inventory_policy_id,
                "mode": mode.value,
                "previous_specification_set_id": previous_specification_set_id,
            }
        ),
        source_snapshot_id=snapshot,
        source_content_digest=snapshot,
        origin_attestation_id=origin_attestation_id,
        mode=mode,
        output_provider="openspec",
        skill_set_id=skill_set.skill_set_id,
        routing_policy_id="deterministic-static-local@1",
        redaction_policy_id="sensitive-content-digest-only@1",
        egress_policy_id="none@1",
        included_paths=tuple(item.path for item in inventory.entries),
        excluded_paths=inventory.excluded_directories,
        facets=tuple(sorted({item.facet for item in surfaces})),
        previous_specification_set_id=previous_specification_set_id,
    )
    evidence_by_id = {item.evidence_id: item for item in evidence}
    definitions_by_skill: dict[str, list[StaticObservationDefinition]] = {}
    for definition in definitions:
        definitions_by_skill.setdefault(definition.skill_id, []).append(definition)
    stage_runs: list[dict[str, Any]] = []

    def execute_skill(executing_skill, _request, _surfaces, _evidence):
        emitted = tuple(
            BehaviorObservation(
                observation_id=item.observation_id,
                surface_ids=item.surface_ids,
                facet=item.facet,
                claim_kind=item.claim_kind,
                statement=item.statement,
                evidence=tuple(evidence_by_id[value] for value in item.evidence_ids),
                skill=executing_skill.ref,
                confidence=item.confidence,
                model_call_id=None,
            )
            for item in definitions_by_skill.get(executing_skill.skill_id, [])
        )
        stage_runs.append(
            {
                "skill": canonical_value(executing_skill.ref),
                "executor": "deterministic-static@1",
                "observation_ids": [item.observation_id for item in emitted],
                "model_call_id": None,
            }
        )
        return emitted

    scopes = {item.observation_id: item.scope for item in definitions}

    def render_draft(_request, observations, _uncertainty):
        statements = tuple(
            DraftStatement(
                statement_id=f"statement:{item.observation_id}",
                capability=f"{item.facet.title()} {_short_id(item.observation_id)}",
                requirement=item.statement,
                scenarios=(
                    DraftScenario(
                        "Exact source evidence",
                        "the exact source snapshot is inventoried",
                        item.statement,
                    ),
                ),
                observation_ids=(item.observation_id,),
            )
            for item in observations
            if scopes[item.observation_id] == "base"
            and item.claim_kind in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
        )
        artifacts = (
            DraftArtifact(
                "specs/derived/spec.md", render_openspec_markdown(statements)
            ),
        )
        return statements, artifacts

    result = SourceToSpecificationWorkflow().run(
        request=request,
        skill_set=skill_set,
        skill_catalog=skill_catalog,
        surfaces=surfaces,
        evidence=evidence,
        execute_skill=execute_skill,
        render_draft=render_draft,
        validate_provider=validate_openspec_draft,
        component_definition_draft=ComponentDefinitionDraft(
            coordinate=f"local/{component_name(root)}",
            title=component_name(root).replace("-", " ").title(),
            provided_capabilities=tuple(
                sorted({f"behavior.{item.facet}" for item in surfaces})
            ),
            source_snapshot_id=snapshot,
        ),
        source_root_guard=root,
    )
    return StaticDerivation(
        result=result,
        flavor_drafts=_flavor_drafts(result, definitions, signals),
        stage_runs=tuple(stage_runs),
    )


__all__ = ["StaticDerivation", "derive_static_checkout"]
