"""File-oriented source-to-specification CLI use-case adapters."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from argparse import Namespace
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from literate_ai.adapters.component_markdown import (
    parse_component_markdown,
    render_component_markdown,
)
from literate_ai.adapters.flavor_markdown import FLAVOR_MARKDOWN_SCHEMA
from literate_ai.contracts import (
    AuthoredProvidedCapability,
    CapabilityRequirement,
    ComponentAuthoring,
    ComponentAuthorityState,
    ComponentContentSelector,
    ComponentCoordinate,
    ComponentGenerationClosure,
    ContentIdentity,
    Entrypoint,
    FlavorSlot,
    RepositoryParentSelection,
    SourceIntelligenceStage,
    canonical_identity,
    component_kind_from_reviewed_node,
)
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.project_source_index import (
    ProjectSourceIntelligenceError,
    require_lifecycle_project_index,
)
from literate_ai.projects import (
    ProjectConfigurationStore,
    ProjectError,
    discover_project,
    source_to_specification_skill_paths,
)
from literate_ai.source_to_specification import (
    INVERSE_EVIDENCE_CUSTODY_SCHEMA,
    LEGACY_MODEL_TRANSLATION_MODE,
    LEGACY_SEMANTIC_SOURCE_TRANSLATION_RUN_SCHEMA,
    LEGACY_SOURCE_TRANSLATION_RUN_SCHEMA,
    MODEL_TRANSLATION_MODE,
    PREVIOUS_SOURCE_TRANSLATION_RUN_SCHEMA,
    SOURCE_TRANSLATION_RUN_SCHEMA,
    BehaviorObservation,
    BehaviorSurface,
    ClaimKind,
    ComponentDefinitionDraft,
    ComponentGraphDraft,
    ComponentGraphNodeDraft,
    CoverageState,
    DraftArtifact,
    DraftScenario,
    DraftStatement,
    EvidenceReference,
    LocalQualificationProfile,
    PromotionPolicy,
    ProviderValidation,
    ReviewAuthority,
    ReviewDisposition,
    ReviewStatementDecision,
    RunMode,
    SourceToSpecificationError,
    SourceToSpecificationRequest,
    SourceToSpecificationResult,
    SourceToSpecificationWorkflow,
    SourceTreeFingerprint,
    SpecificationReviewDecision,
    builtin_skill_set,
    canonical_digest,
    canonical_value,
    derive_static_checkout,
    generation_input_subset_identity,
    inventory_source,
    load_builtin_skill_catalog,
    load_skill_catalog,
    load_skill_manifests,
    parse_source_attestation,
    plan_refresh,
    promote,
    read_local_trust_key,
    sign_review,
    sign_source_inventory,
    validate_component_graph_projection,
    validate_inverse_evidence_custody,
    validate_model_translation_record,
    validate_qualification_inverse_evidence,
    verify_review_attestation,
    verify_source_attestation,
)
from literate_ai.source_to_specification.synthesis import (
    capability_contract_path,
    promoted_component_specification_projection,
    render_capability_contract_markdown,
    validate_openspec_draft,
)

from ._wire import (
    component_graph_from_wire,
    result_from_wire,
    result_to_wire,
    review_from_wire,
    review_to_wire,
)
from .errors import CLI_RESULT_SCHEMA, CliFailure, JsonArgumentParser, _json_text

CASE_SCHEMA = "literate-ai/source-to-specification-case@1"
BUNDLE_SCHEMA = "literate-ai/source-to-specification-result-bundle@5"
PREVIOUS_BUNDLE_SCHEMA = "literate-ai/source-to-specification-result-bundle@4"
OLDER_BUNDLE_SCHEMA = "literate-ai/source-to-specification-result-bundle@3"
V2_BUNDLE_SCHEMA = "literate-ai/source-to-specification-result-bundle@2"
LEGACY_BUNDLE_SCHEMA = "literate-ai/source-to-specification-result-bundle@1"
CONFORMANCE_SCHEMA = "literate-ai/source-to-specification-conformance@1"


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CliFailure("cli.invalid_input", f"{name} must be a JSON object")
    return value


def _array(value: Any, name: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise CliFailure("cli.invalid_input", f"{name} must be a JSON array")
    return tuple(value)


def _strings(value: Any, name: str) -> tuple[str, ...]:
    items = _array(value, name)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise CliFailure("cli.invalid_input", f"{name} must contain non-empty strings")
    return items


def _read_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise CliFailure(
            "cli.input_unavailable", f"input is not a regular file: {path}"
        )
    try:
        return _object(json.loads(source.read_text()), str(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliFailure(
            "cli.invalid_json", f"could not read JSON input: {path}"
        ) from exc


def _write_new_json(path: Path, value: object) -> None:
    if path.exists() or path.is_symlink():
        raise CliFailure("cli.output_exists", "output path must not already exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        staging.write_text(_json_text(value), encoding="utf-8", newline="\n")
        os.replace(staging, path)
    except BaseException:
        if staging.exists():
            staging.unlink()
        raise


def _load_case(path: str | Path) -> tuple[Path, dict[str, Any]]:
    descriptor = Path(path).resolve()
    data = _read_json(descriptor)
    if data.get("schema") != CASE_SCHEMA:
        raise CliFailure("cli.unsupported_case", "unsupported conformance case schema")
    required = {
        "schema",
        "case_id",
        "source_root",
        "surfaces",
        "evidence",
        "observations",
        "expected",
    }
    optional = {
        "flavor_drafts",
        "previous_case",
        "changed_source_paths",
        "origin_id",
    }
    unknown = set(data) - required - optional
    missing = required - data.keys()
    if missing or unknown:
        detail = "missing " + ", ".join(sorted(missing)) if missing else ""
        if unknown:
            detail += ("; " if detail else "") + "unknown " + ", ".join(sorted(unknown))
        raise CliFailure("cli.invalid_case", f"invalid case fields: {detail}")
    if not isinstance(data["case_id"], str) or not data["case_id"].strip():
        raise CliFailure("cli.invalid_case", "case_id must be a non-empty string")
    return descriptor, data


def _contained(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _source_root(descriptor: Path, case: dict[str, Any]) -> Path:
    raw = case["source_root"]
    if not isinstance(raw, str) or not raw.strip():
        raise CliFailure("cli.invalid_case", "source_root must be a non-empty string")
    root = (descriptor.parent / raw).resolve()
    if not root.is_dir():
        raise CliFailure(
            "cli.source_unavailable", "case source_root is not a directory"
        )
    return root


def _source_identity(root: Path) -> str:
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CliFailure(
                "cli.source_symlink", "analyzed source cannot contain symlinks"
            )
        if not path.is_file():
            continue
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "content_digest": "sha256:"
                + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not entries:
        raise CliFailure(
            "cli.source_empty", "analyzed source contains no regular files"
        )
    return canonical_digest(entries)


def _load_discovered_skill_catalog(
    descriptor: Path | None, explicit: str | None
) -> dict[str, Any]:
    if explicit is not None:
        return load_skill_catalog(Path(explicit).resolve())
    if descriptor is None:
        return load_builtin_skill_catalog()
    try:
        project = discover_project(descriptor)
        if project is None:
            return load_builtin_skill_catalog()
        manifests = source_to_specification_skill_paths(project)
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    return load_skill_manifests(manifests)


def _reviewed_static_skill_catalog(
    explicit: str | None,
    *,
    languages: tuple[str, ...],
) -> dict[str, Any]:
    """Allow only exact packaged built-ins to classify arbitrary source."""

    packaged = load_builtin_skill_catalog()
    catalog = packaged
    if explicit is not None:
        catalog = load_skill_catalog(Path(explicit).resolve())
    selected = builtin_skill_set(catalog, languages=languages)
    mismatched = tuple(
        reference.skill_id
        for reference in selected.skills
        if reference.skill_id not in packaged
        or packaged[reference.skill_id].ref != reference
    )
    if mismatched:
        raise CliFailure(
            "workflow.skill_untrusted",
            "arbitrary source analysis requires exact reviewed built-in skills: "
            + ", ".join(mismatched),
        )
    return catalog


def _standalone_source(path: str | Path) -> Path | None:
    source = Path(path)
    if source.is_dir():
        return source.resolve()
    if source.is_file():
        if source.suffix.lower() == ".json":
            try:
                value = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return source.resolve()
            if isinstance(value, dict) and value.get("schema") == CASE_SCHEMA:
                return None
        return source.resolve()
    return None


def _require_source_to_specification_project_index(
    source: Path,
) -> dict[str, object] | None:
    """Honor the selected source project's stage without mutating its index."""

    try:
        project = discover_project(source)
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if project is None:
        return None
    try:
        return require_lifecycle_project_index(
            project.root,
            project.definition.source_intelligence,
            stage=SourceIntelligenceStage.SOURCE_TO_SPECIFICATION,
            synchronize=False,
        )
    except ProjectSourceIntelligenceError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def _load_attestation(path: str | None) -> Any:
    if path is None:
        return None
    envelope = _read_json(path)
    if envelope.get("schema") == CLI_RESULT_SCHEMA:
        envelope = _object(envelope.get("result"), "CLI attestation result")
    return parse_source_attestation(envelope)


def _derive_standalone(
    source: Path,
    *,
    skills_root: str | None,
    attestation_path: str | None,
    trust_key_path: str | None,
    translator_kind: str = "static",
    allow_model_egress: bool = False,
    model: str | None = None,
    model_evidence_byte_budget: int = 262_144,
    mode: RunMode = RunMode.BOOTSTRAP,
    previous_specification_set_id: str | None = None,
) -> tuple[dict[str, Any], SourceToSpecificationResult]:
    inventory = inventory_source(source)
    languages = tuple(
        sorted(
            {item.language for item in inventory.entries if item.language is not None}
        )
    )
    attestation = _load_attestation(attestation_path)
    origin_verified = False
    if attestation is not None:
        if trust_key_path is None:
            raise CliFailure(
                "cli.trust_key_required",
                "verifying a local source attestation requires --trust-key",
            )
        key = read_local_trust_key(trust_key_path)
        verify_source_attestation(
            attestation,
            expected_source_snapshot_id=inventory.identity,
            key=key,
        )
        origin_verified = True
        origin_id = attestation.identity
    else:
        origin_id = f"unverified-local:{inventory.identity.removeprefix('sha256:')}"
    catalog = _reviewed_static_skill_catalog(
        skills_root,
        languages=languages,
    )
    selected = builtin_skill_set(catalog, languages=languages)
    if translator_kind == "coding-cli":
        del allow_model_egress, model, model_evidence_byte_budget
        raise CliFailure(
            "model_translation.source_intelligence_disabled",
            "model-backed source translation is unavailable; use the static translator",
        )
    else:
        derivation = derive_static_checkout(
            source=source,
            inventory=inventory,
            origin_attestation_id=origin_id,
            skill_set=selected,
            skill_catalog=catalog,
            mode=mode,
            previous_specification_set_id=previous_specification_set_id,
        )
    result = derivation.result
    classifications = Counter(item.classification.value for item in inventory.entries)
    allowed_coverage = {
        CoverageState.COVERED,
        CoverageState.EXCLUDED,
        CoverageState.IMPLEMENTATION_DETAIL,
    }
    blocked_coverage = tuple(
        item.surface_id
        for item in result.coverage.entries
        if item.state not in allowed_coverage
    )
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "case_id": f"standalone:{source.name}",
        "source_kind": "standalone-local",
        "result": result_to_wire(result),
        "source_inventory": canonical_value(inventory),
        "skill_stage_runs": list(derivation.stage_runs),
        "flavor_drafts": list(derivation.flavor_drafts),
        "behavioral_surface_inventory": (
            derivation.surface_inventory.to_dict()
            if translator_kind == "coding-cli"
            else None
        ),
        "evidence_partition_manifest": (
            derivation.evidence_partition_manifest.to_dict()
            if translator_kind == "coding-cli"
            else None
        ),
        "evidence_batch_plan": (
            derivation.evidence_batch_plan.to_dict()
            if translator_kind == "coding-cli"
            else None
        ),
        "translation": (
            {
                "schema": SOURCE_TRANSLATION_RUN_SCHEMA,
                "mode": MODEL_TRANSLATION_MODE,
                "intelligence": derivation.intelligence.to_dict(include_content=True),
                "journals": [item.to_dict() for item in derivation.journals],
            }
            if translator_kind == "coding-cli"
            else {
                "schema": SOURCE_TRANSLATION_RUN_SCHEMA,
                "mode": "deterministic-static",
                "intelligence": None,
                "journals": [],
            }
        ),
        "security": {
            "origin_verified": origin_verified,
            "origin_attestation": (
                attestation.to_dict() if attestation is not None else None
            ),
            "egress_policy_id": result.request.egress_policy_id,
            "redaction_policy_id": result.request.redaction_policy_id,
            "file_classification_counts": {
                key: classifications[key] for key in sorted(classifications)
            },
            "prompt_injection_paths": [
                item.path for item in inventory.entries if item.prompt_injection
            ],
            "sensitive_paths": [
                item.path
                for item in inventory.entries
                if item.classification.value == "sensitive"
            ],
            "dynamic_observation": "not-executed",
            "model_egress_authorized": (
                allow_model_egress if translator_kind == "coding-cli" else False
            ),
        },
        "review_gate": {
            "promotion_policy_id": "signed-local-human-reviewed@1",
            "requires_signed_review": True,
            "blocking_uncertainty_ids": [
                item.uncertainty_id
                for item in result.uncertainty.items
                if item.blocking
            ],
            "blocked_coverage_surface_ids": list(blocked_coverage),
            "promotion_eligible": origin_verified and not blocked_coverage,
        },
    }
    return bundle, result


def _build_inputs(
    descriptor: Path,
    case: dict[str, Any],
    *,
    mode: RunMode,
    previous_specification_set_id: str | None,
    skills_root: str | None,
) -> tuple[
    SourceToSpecificationRequest,
    Any,
    dict[str, Any],
    tuple[BehaviorSurface, ...],
    tuple[EvidenceReference, ...],
    Path,
]:
    root = _source_root(descriptor, case)
    snapshot = _source_identity(root)
    try:
        catalog = _load_discovered_skill_catalog(descriptor, skills_root)
        selected = builtin_skill_set(catalog)
    except SourceToSpecificationError as exc:
        raise CliFailure(exc.code, exc.message) from exc

    evidence_items: list[EvidenceReference] = []
    for raw in _array(case["evidence"], "evidence"):
        item = _object(raw, "evidence item")
        path_text = item.get("path")
        if not isinstance(path_text, str):
            raise CliFailure("cli.invalid_case", "evidence path must be a string")
        source_path = (root / path_text).resolve()
        if not _contained(root, source_path) or not source_path.is_file():
            raise CliFailure(
                "cli.evidence_unavailable",
                f"evidence path is outside or absent: {path_text}",
            )
        evidence_items.append(
            EvidenceReference(
                evidence_id=item["evidence_id"],
                source_snapshot_id=snapshot,
                content_digest="sha256:"
                + hashlib.sha256(source_path.read_bytes()).hexdigest(),
                path=path_text,
                symbol=item.get("symbol", ""),
            )
        )
    evidence_tuple = tuple(evidence_items)
    included_paths = tuple(sorted({item.path for item in evidence_tuple}))

    surface_items: list[BehaviorSurface] = []
    for raw in _array(case["surfaces"], "surfaces"):
        item = _object(raw, "surface")
        declared = item.get("declared_state")
        surface_items.append(
            BehaviorSurface(
                surface_id=item["surface_id"],
                facet=item["facet"],
                description=item["description"],
                declared_state=None if declared is None else CoverageState(declared),
                reason=item.get("reason", ""),
            )
        )
    surfaces = tuple(surface_items)
    facets = tuple(sorted({item.facet for item in surfaces}))
    request_payload = {
        "case_id": case["case_id"],
        "snapshot": snapshot,
        "mode": mode.value,
        "previous": previous_specification_set_id,
        "skill_set": selected.identity,
    }
    source_request = SourceToSpecificationRequest(
        request_id=canonical_digest(request_payload),
        source_snapshot_id=snapshot,
        source_content_digest=snapshot,
        origin_attestation_id=f"sample-origin:{case.get('origin_id', case['case_id'])}",
        mode=mode,
        output_provider="openspec",
        skill_set_id=selected.skill_set_id,
        routing_policy_id="sample-static-local@1",
        redaction_policy_id="sample-no-egress@1",
        egress_policy_id="none@1",
        included_paths=included_paths,
        facets=facets,
        previous_specification_set_id=previous_specification_set_id,
    )
    return source_request, selected, catalog, surfaces, evidence_tuple, root


def _render_markdown(statements: tuple[DraftStatement, ...]) -> str:
    lines = ["# Derived specification", ""]
    if not statements:
        lines.extend(["No normative requirement could be supported.", ""])
    for statement in statements:
        scenario = statement.scenarios[0]
        lines.extend(
            [
                f"## Requirement: {statement.capability}",
                "",
                statement.requirement,
                "",
                f"### Scenario: {scenario.name}",
                "",
                f"- **WHEN** {scenario.when}",
                f"- **THEN** {scenario.then}",
                "",
            ]
        )
    return "\n".join(lines)


def _derive(
    descriptor: Path,
    case: dict[str, Any],
    *,
    mode: RunMode,
    previous_specification_set_id: str | None,
    skills_root: str | None,
) -> tuple[dict[str, Any], SourceToSpecificationResult, dict[str, str]]:
    (
        source_request,
        selected,
        catalog,
        surfaces,
        evidence,
        source_root,
    ) = _build_inputs(
        descriptor,
        case,
        mode=mode,
        previous_specification_set_id=previous_specification_set_id,
        skills_root=skills_root,
    )
    evidence_by_id = {item.evidence_id: item for item in evidence}
    scopes: dict[str, str] = {}
    definitions_by_skill: dict[str, list[dict[str, Any]]] = {}
    for raw in _array(case["observations"], "observations"):
        item = _object(raw, "observation")
        skill_id = item.get("skill_id")
        if skill_id not in catalog:
            raise CliFailure(
                "cli.observation_skill_unknown",
                f"observation names unavailable independent skill: {skill_id}",
            )
        definitions_by_skill.setdefault(skill_id, []).append(item)
        scopes[item["observation_id"]] = item.get("draft_scope", "base")

    def execute_skill(executing_skill, _request, _surfaces, _evidence):
        emitted = []
        for item in definitions_by_skill.get(executing_skill.skill_id, []):
            claim = ClaimKind(item["claim_kind"])
            references = tuple(
                evidence_by_id[evidence_id]
                for evidence_id in _strings(item["evidence_ids"], "evidence_ids")
            )
            emitted.append(
                BehaviorObservation(
                    observation_id=item["observation_id"],
                    surface_ids=_strings(item["surface_ids"], "surface_ids"),
                    facet=item["facet"],
                    claim_kind=claim,
                    statement=item["statement"],
                    evidence=references,
                    skill=executing_skill.ref,
                    confidence=item["confidence"],
                    model_call_id=None,
                )
            )
        return tuple(emitted)

    def render_draft(_request, observations, _uncertainty):
        statements = tuple(
            DraftStatement(
                statement_id=f"statement:{observation.observation_id}",
                capability=observation.facet,
                requirement=observation.statement,
                scenarios=(
                    DraftScenario(
                        name="Source-backed behavior",
                        when="the observed surface is exercised",
                        then=observation.statement,
                    ),
                ),
                observation_ids=(observation.observation_id,),
            )
            for observation in observations
            if scopes[observation.observation_id] == "base"
            and observation.claim_kind
            in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
        )
        return statements, (
            DraftArtifact(
                path="specs/derived/spec.md", content=_render_markdown(statements)
            ),
        )

    def validate_provider(provider, artifacts):
        valid = bool(artifacts) and all(
            artifact.path.startswith("specs/") and artifact.content.strip()
            for artifact in artifacts
        )
        return ProviderValidation(
            provider=provider,
            provider_version="sample-validator@1",
            valid=valid,
            diagnostics=() if valid else ("invalid generated artifact tree",),
        )

    result = SourceToSpecificationWorkflow().run(
        request=source_request,
        skill_set=selected,
        skill_catalog=catalog,
        surfaces=surfaces,
        evidence=evidence,
        execute_skill=execute_skill,
        render_draft=render_draft,
        validate_provider=validate_provider,
        component_definition_draft=ComponentDefinitionDraft(
            coordinate=f"sample/{case['case_id']}",
            title=case["case_id"].replace("-", " ").title(),
            provided_capabilities=facets_to_capabilities(surfaces),
            source_snapshot_id=source_request.source_snapshot_id,
        ),
        source_root_guard=source_root,
    )
    flavor_drafts = _flavor_drafts(case, result, scopes)
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "case_id": case["case_id"],
        "result": result_to_wire(result),
        "flavor_drafts": flavor_drafts,
        "behavioral_surface_inventory": None,
        "evidence_partition_manifest": None,
        "evidence_batch_plan": None,
    }
    return bundle, result, {item.evidence_id: item.content_digest for item in evidence}


def facets_to_capabilities(
    surfaces: tuple[BehaviorSurface, ...],
) -> tuple[str, ...]:
    return tuple(sorted({f"behavior.{item.facet}" for item in surfaces}))


def _flavor_drafts(
    case: dict[str, Any],
    result: SourceToSpecificationResult,
    scopes: dict[str, str],
) -> list[dict[str, Any]]:
    observations = {item.observation_id: item for item in result.observations}
    drafts: list[dict[str, Any]] = []
    for raw in _array(case.get("flavor_drafts", []), "flavor_drafts"):
        item = _object(raw, "flavor draft")
        scope = f"flavor:{item['flavor_id']}"
        selected = tuple(
            observation
            for observation in result.observations
            if scopes[observation.observation_id] == scope
        )
        statements = tuple(
            DraftStatement(
                statement_id=f"flavor-statement:{observation.observation_id}",
                capability=observation.facet,
                requirement=observation.statement,
                scenarios=(
                    DraftScenario(
                        name="Flavor-specific source behavior",
                        when="the Flavor is selected",
                        then=observation.statement,
                    ),
                ),
                observation_ids=(observation.observation_id,),
            )
            for observation in selected
            if observation.claim_kind
            in {ClaimKind.OBSERVED, ClaimKind.COMPATIBILITY_QUIRK}
        )
        observation_ids = tuple(item.observation_id for item in selected)
        if any(
            observation_id not in observations for observation_id in observation_ids
        ):
            raise CliFailure("cli.invalid_case", "Flavor draft observation is unknown")
        payload = {
            "flavor_id": item["flavor_id"],
            "axis": item["axis"],
            "observation_ids": observation_ids,
            "statements": statements,
        }
        drafts.append(
            {
                "flavor_draft_id": canonical_digest(payload),
                "flavor_id": item["flavor_id"],
                "axis": item["axis"],
                "observation_ids": list(observation_ids),
                "statements": canonical_value(statements),
            }
        )
    return drafts


def _unwrap_bundle(
    path: str | Path,
) -> tuple[dict[str, Any], SourceToSpecificationResult]:
    data = _read_json(path)
    if data.get("schema") == CLI_RESULT_SCHEMA:
        data = _object(data.get("result"), "CLI result")
    if data.get("schema") in {
        "literate-ai/source-to-specification-audit@1",
        "literate-ai/source-to-specification-refresh@1",
    }:
        data = _object(data.get("bundle"), "nested result bundle")
    if data.get("schema") not in {
        BUNDLE_SCHEMA,
        PREVIOUS_BUNDLE_SCHEMA,
        OLDER_BUNDLE_SCHEMA,
        V2_BUNDLE_SCHEMA,
        LEGACY_BUNDLE_SCHEMA,
    }:
        raise CliFailure("cli.unsupported_bundle", "input is not a result bundle")
    try:
        return data, result_from_wire(data["result"])
    except (KeyError, TypeError, ValueError, SourceToSpecificationError) as exc:
        raise CliFailure("cli.invalid_bundle", "result bundle is invalid") from exc


def _unwrap_review(path: str | Path) -> SpecificationReviewDecision:
    data = _read_json(path)
    if data.get("schema") == CLI_RESULT_SCHEMA:
        data = _object(data.get("result"), "CLI result")
    if data.get("schema") != "literate-ai/specification-review-decision@1":
        raise CliFailure("cli.unsupported_review", "input is not a review decision")
    try:
        return review_from_wire(data["review"])
    except (KeyError, TypeError, ValueError, SourceToSpecificationError) as exc:
        raise CliFailure("cli.invalid_review", "review decision is invalid") from exc


def _read_reviewed_component_graph(path: str | Path) -> ComponentGraphDraft:
    try:
        graph = component_graph_from_wire(_read_json(path))
    except (KeyError, TypeError, ValueError, SourceToSpecificationError) as exc:
        raise CliFailure(
            "review.component_graph_invalid",
            "reviewed Component graph is invalid",
        ) from exc
    return graph


def _static_review_artifacts(
    result: SourceToSpecificationResult,
    graph: ComponentGraphDraft,
) -> tuple[tuple[DraftArtifact, ...], ProviderValidation]:
    """Add only reviewer-authored public contracts to a static draft tree."""

    if len(graph.nodes) != 1:
        raise CliFailure(
            "review.component_graph_static_multiple_nodes",
            "static review supports one explicit Component boundary per promotion",
        )
    if graph.nodes[0].kind == "unknown":
        raise CliFailure(
            "review.component_graph_semantics_unknown",
            "static review requires an explicit library, CLI, or service kind",
        )
    artifacts = {item.path: item for item in result.draft.artifacts}
    for contract in graph.nodes[0].capability_contracts:
        path = capability_contract_path(contract.name)
        artifact = DraftArtifact(path, render_capability_contract_markdown(contract))
        existing = artifacts.get(path)
        if existing is not None and existing != artifact:
            raise CliFailure(
                "review.component_contract_conflict",
                f"reviewed capability contract conflicts with {path}",
            )
        artifacts[path] = artifact
    reviewed = tuple(artifacts[path] for path in sorted(artifacts))
    validation = validate_openspec_draft(result.draft.output_provider, reviewed)
    if not validation.valid:
        raise CliFailure(
            "review.component_graph_validation_failed",
            "reviewed static Component graph did not produce a valid specification",
        )
    return reviewed, validation


def _artifact_map(path: str | Path) -> dict[str, str]:
    data = _read_json(path)
    if data.get("schema") == CLI_RESULT_SCHEMA:
        data = _object(data.get("result"), "CLI result")
    if data.get("schema") in {
        "literate-ai/source-to-specification-audit@1",
        "literate-ai/source-to-specification-refresh@1",
    }:
        data = _object(data.get("bundle"), "nested result bundle")
    if data.get("schema") in {
        BUNDLE_SCHEMA,
        PREVIOUS_BUNDLE_SCHEMA,
        OLDER_BUNDLE_SCHEMA,
        V2_BUNDLE_SCHEMA,
        LEGACY_BUNDLE_SCHEMA,
    }:
        draft = _object(_object(data["result"], "result")["draft"], "draft")
        artifacts = _array(draft["artifacts"], "artifacts")
    elif data.get("schema") == "literate-ai/accepted-specification-set@1":
        artifacts = _array(data["artifacts"], "artifacts")
    else:
        raise CliFailure("cli.unsupported_diff_input", "diff input is not supported")
    return {
        item["path"]: item["content"]
        for raw in artifacts
        for item in (_object(raw, "artifact"),)
    }


def _raw_reference(kind: str, uri: str, content: bytes) -> dict[str, object]:
    return {
        "schema": "urn:literate-ai:schema:v1:content-reference",
        "kind": kind,
        "uri": uri,
        "identity": {
            "schema": "urn:literate-ai:schema:v1:content-identity",
            "algorithm": "sha256",
            "digest": hashlib.sha256(content).hexdigest(),
        },
    }


def _authoring_selector(reference: dict[str, object]) -> ComponentContentSelector:
    """Project one byte-pinned local reference into readable Component intent."""

    return ComponentContentSelector(
        str(reference["kind"]),
        str(reference["uri"]),
        ContentIdentity.from_dict(reference["identity"]),
    )


def _coordinate_parts(raw: str, fallback: str) -> tuple[str, str]:
    parts = raw.split("/", 1)
    namespace = parts[0] if len(parts) == 2 else "local"
    name = parts[-1] or fallback
    normalized = tuple(
        re.sub(r"[^a-z0-9._-]+", "-", item.lower()).strip("-._")
        for item in (namespace, name)
    )
    if not all(normalized):
        raise CliFailure(
            "promotion.component_coordinate_invalid",
            "derived Component coordinate cannot be normalized safely",
        )
    return normalized


def _reviewed_node_manifest_semantics(
    node: ComponentGraphNodeDraft,
) -> tuple[
    tuple[str, ...],
    tuple[AuthoredProvidedCapability, ...],
    tuple[Entrypoint, ...],
    tuple[str, ...],
]:
    """Project reviewed node semantics without introducing manifest defaults."""

    if node.kind == "unknown":
        raise CliFailure(
            "promotion.component_semantics_unknown",
            f"Component {node.coordinate} lacks reviewed manifest semantics",
        )
    return (
        tuple(sorted({*node.profiles, node.kind, "source-derived"})),
        tuple(
            AuthoredProvidedCapability(
                item.name,
                "1.0.0",
                ComponentContentSelector(
                    "public-interface-contract",
                    capability_contract_path(item.name),
                    None,
                ),
            )
            for item in node.capability_contracts
        ),
        tuple(Entrypoint(item.name, item.kind, item.path) for item in node.entrypoints),
        node.build_needs,
    )


def _flavor_markdown(draft: dict[str, Any]) -> str:
    lines = [
        f"# {str(draft['flavor_id']).title()} implementation Flavor",
        "",
        "## ADDED Requirements",
        "",
    ]
    for raw in _array(draft.get("statements", []), "Flavor statements"):
        statement = _object(raw, "Flavor statement")
        capability = str(statement.get("capability", "implementation behavior"))
        statement_id = str(statement.get("statement_id", "source-backed-statement"))
        lines.extend(
            [
                f"### Requirement: {capability} [{statement_id}]",
                "",
                str(statement.get("requirement", "")),
                "",
            ]
        )
        for raw_scenario in _array(statement.get("scenarios", []), "Flavor scenarios"):
            scenario = _object(raw_scenario, "Flavor scenario")
            lines.extend(
                [
                    f"#### Scenario: {scenario.get('name', 'Source-backed behavior')}",
                    "",
                    f"- **WHEN** {scenario.get('when', 'the Flavor is selected')}",
                    "- **THEN** "
                    + str(scenario.get("then", statement.get("requirement", ""))),
                    "",
                ]
            )
    return "\n".join(lines)


def _create_promoted_project(
    *,
    accepted_root: Path,
    project_target: Path,
    result: SourceToSpecificationResult,
    flavor_drafts: object,
    project_id: str | None,
    qualification_profile: Path | None,
    qualification_target: str,
    qualification_flavors: tuple[str, ...],
    specification_set_id: str,
    review_id: str,
    translation: object | None,
    inverse_evidence_custody: object | None,
    parent_selection: RepositoryParentSelection | None,
) -> dict[str, object]:
    """Create one complete, disposable-source-ready project from an accepted set."""

    from .project import init_project_from_args

    if project_target.exists() or project_target.is_symlink():
        raise CliFailure(
            "promotion.project_target_exists",
            "promoted project target must not already exist",
        )
    draft = result.component_definition_draft
    if draft is None:
        raise CliFailure(
            "promotion.component_draft_missing",
            "a complete project requires a source-derived Component draft",
        )
    if (
        not isinstance(translation, dict)
        or translation.get("schema") != SOURCE_TRANSLATION_RUN_SCHEMA
        or translation.get("mode")
        not in {
            MODEL_TRANSLATION_MODE,
            "deterministic-static",
        }
    ):
        raise CliFailure(
            "promotion.component_semantics_unknown",
            "source-free project promotion requires current semantic translation or "
            "a signed reviewed static Component graph",
        )
    graph = result.component_graph_draft
    if graph is None:
        raise CliFailure(
            "promotion.component_semantics_unknown",
            "project promotion requires a reviewed Component graph with explicit "
            "kind, profiles, capability contracts, entrypoints, and build needs",
        )
    if any(node.kind == "unknown" for node in graph.nodes):
        raise CliFailure(
            "promotion.component_semantics_unknown",
            "legacy Component graph lacks reviewed kind, profiles, capability "
            "contracts, entrypoints, or build needs",
        )
    namespace, name = _coordinate_parts(graph.root_coordinate, accepted_root.name)
    parent = project_target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{project_target.name}.", dir=parent))
    shutil.rmtree(staging)
    try:
        from literate_ai.adapters.project_initialization import (
            KNOWN_FLAVOR_SELECTORS,
            canonical_flavor_selector,
            flavor_selector_directory,
        )

        host_flavor = (
            "windows"
            if sys.platform == "win32"
            else "macos"
            if sys.platform == "darwin"
            else "linux"
        )
        bootstrap_flavors = {
            "flavor://literate-ai/build-bazel",
            f"flavor://literate-ai/os-{host_flavor}",
        }
        for selector in qualification_flavors:
            if not selector.startswith("+"):
                continue
            selected = canonical_flavor_selector(selector)[1:]
            if selected in KNOWN_FLAVOR_SELECTORS:
                bootstrap_flavors.add(selected)
        init_project_from_args(
            Namespace(
                path=str(staging),
                project_id=project_id or f"{namespace}-{name}",
                profile="canonical",
                flavors=sorted(bootstrap_flavors),
                empty=True,
                parent_selection=parent_selection,
            )
        )
        # Canonical initialization includes a tutorial Component.  A promotion is
        # itself the complete application graph, so retaining that unrelated lock
        # would both pollute the graph and become stale when reviewed Flavors replace
        # template Flavors at the same paths.
        starter_component = staging / "samples" / "hello-component"
        if starter_component.is_dir() and not starter_component.is_symlink():
            shutil.rmtree(starter_component)
        workflow_path = (
            staging / "workflows" / "production" / "staging" / "dev" / "workflow.md"
        )
        routing_path = (
            staging / "routing" / "production" / "staging" / "dev" / "routing.json"
        )
        skill_path = (
            staging
            / "skills"
            / "specification-to-source"
            / "portable-application"
            / "SKILL.md"
        )
        implementation_skill_path = (
            staging
            / "skills"
            / "specification-to-source"
            / "portable-application-implementation"
            / "SKILL.md"
        )
        planning_skill_path = (
            staging
            / "skills"
            / "specification-to-source"
            / "portable-specification-planning"
            / "SKILL.md"
        )
        workflow_ref = _raw_reference(
            "workflow",
            "workflows/production/staging/dev/workflow.md",
            workflow_path.read_bytes(),
        )
        routing_ref = _raw_reference(
            "routing-policy",
            "routing/production/staging/dev/routing.json",
            routing_path.read_bytes(),
        )
        skill_ref = _raw_reference(
            "specification-to-source-skill",
            "skills/specification-to-source/portable-application/SKILL.md",
            skill_path.read_bytes(),
        )
        # A selected language Flavor contributes its own portable-application
        # skill (e.g. python-portable-application), and every such skill
        # declares portable-application-implementation as its one dependency,
        # which in turn declares portable-specification-planning. Both must
        # already be present in the base authoring inputs for
        # _resolved_recipe_skills to admit the flavor's skill.
        implementation_skill_ref = _raw_reference(
            "specification-to-source-skill",
            "skills/specification-to-source/portable-application-implementation/"
            "SKILL.md",
            implementation_skill_path.read_bytes(),
        )
        planning_skill_ref = _raw_reference(
            "specification-to-source-skill",
            "skills/specification-to-source/portable-specification-planning/SKILL.md",
            planning_skill_path.read_bytes(),
        )
        root_acceptance_contracts: list[dict[str, object]] = []
        profile_content: bytes | None = None
        if qualification_profile is not None:
            profile_value = _read_json(qualification_profile)
            LocalQualificationProfile.from_dict(profile_value)
            profile_content = _json_text(profile_value).encode("utf-8")
            root_acceptance_contracts.append(
                _raw_reference(
                    "acceptance-contract",
                    "qualification/profile.json",
                    profile_content,
                )
            )
        artifacts = {item.path: item for item in result.draft.artifacts}
        if not artifacts:
            raise CliFailure(
                "promotion.specification_missing",
                "accepted Component has no specification artifacts",
            )
        proposals = tuple(
            _object(item, "Flavor draft")
            for item in _array(
                flavor_drafts if isinstance(flavor_drafts, list) else [],
                "Flavor drafts",
            )
        )
        axes = tuple(
            sorted(
                {
                    str(item.get("axis", "implementation.language-ecosystem"))
                    for item in proposals
                }
            )
        )
        node_coordinates: dict[str, tuple[str, str]] = {
            node.coordinate: _coordinate_parts(node.coordinate, accepted_root.name)
            for node in graph.nodes
        }
        component_names = tuple(value[1] for value in node_coordinates.values())
        if len(component_names) != len(set(component_names)):
            raise CliFailure(
                "promotion.component_name_collision",
                "recovered Component coordinates map to duplicate project names",
            )
        graph_capabilities = tuple(
            sorted(
                {
                    capability
                    for node in graph.nodes
                    for capability in node.provided_capabilities
                }
            )
        )
        component_revisions: dict[str, ContentIdentity] = {}
        from literate_ai.source_to_specification.promotion_materialization import (
            PromotionInputKind,
            SourcePromotionInput,
            SourcePromotionMaterializer,
        )

        materializer = SourcePromotionMaterializer()
        promotion_audits: list[tuple[str, object]] = []
        component_specifications: dict[str, tuple[DraftArtifact, ...]] = {}
        authored_components: list[str] = []

        for node in graph.nodes:
            node_namespace, node_name = node_coordinates[node.coordinate]
            component_root = staging / "components" / node_name
            try:
                specification_projection = promoted_component_specification_projection(
                    provider=result.draft.output_provider,
                    graph=graph,
                    artifacts=result.draft.artifacts,
                    coordinate=node.coordinate,
                )
            except ValueError as exc:
                raise CliFailure(
                    "promotion.component_specification_missing",
                    f"accepted tree omits the specification for {node.coordinate}",
                ) from exc
            promoted_artifacts = tuple(
                item for _source_path, item in specification_projection
            )
            component_specifications[node.coordinate] = promoted_artifacts
            for source_path, _promoted_artifact in specification_projection:
                accepted_specification = accepted_root.joinpath(
                    *Path(source_path).parts
                )
                if not accepted_specification.is_file():
                    raise CliFailure(
                        "promotion.component_specification_missing",
                        f"reviewed accepted tree omits {source_path}",
                    )
            promotion_inputs = tuple(
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted_root,
                    source_root_label="accepted-specifications",
                    source_path=source_path,
                    target_path=promoted_artifact.path,
                    expected_content_identity=(
                        "sha256:"
                        + hashlib.sha256(
                            promoted_artifact.content.encode("utf-8")
                        ).hexdigest()
                    ),
                )
                for source_path, promoted_artifact in specification_projection
            )
            materializer.materialize(
                promotion_inputs,
                component_root,
            )
            if node.coordinate == graph.root_coordinate:
                if profile_content is not None:
                    promoted_profile = component_root / "qualification" / "profile.json"
                    promoted_profile.parent.mkdir(parents=True, exist_ok=True)
                    promoted_profile.write_bytes(profile_content)
            outgoing = tuple(
                edge
                for edge in graph.edges
                if edge.source_coordinate == node.coordinate
            )
            capabilities = tuple(sorted(node.provided_capabilities))
            unsupported_build_needs = set(node.build_needs) - {
                *axes,
                "build.system",
            }
            if unsupported_build_needs:
                raise CliFailure(
                    "promotion.component_build_need_unresolved",
                    f"Component {node.coordinate} requires an unavailable Flavor axis "
                    f"{sorted(unsupported_build_needs)[0]!r}",
                )
            profiles, provided_contracts, entrypoints, build_needs = (
                _reviewed_node_manifest_semantics(node)
            )
            public_interface_paths = {
                capability_contract_path(item.name)
                for item in node.capability_contracts
            }
            authoring = ComponentAuthoring(
                library_imports=node.library_imports,
                coordinate=ComponentCoordinate(node_namespace, node_name),
                version="1.0.0",
                display_name=node.title,
                kind=(
                    component_kind_from_reviewed_node(node.kind).value
                    if node.kind == "library"
                    else ""
                ),
                description=(
                    "Source-derived Component. Intent authority is the accepted "
                    "specification; release implementation authority remains with the "
                    "recorded source baseline until regenerative qualification passes."
                ),
                profiles=profiles,
                sample=False,
                provides=provided_contracts,
                requires=tuple(
                    CapabilityRequirement.from_dict(
                        {
                            "schema": (
                                "urn:literate-ai:schema:v1:capability-requirement"
                            ),
                            "requirement_id": edge.requirement_id,
                            "capability": edge.capability,
                            "version_range": edge.version_range,
                            "dependency_kind": edge.dependency_kind,
                            "optional": edge.optional,
                            "constraints": [],
                        }
                    )
                    for edge in sorted(outgoing, key=lambda item: item.requirement_id)
                ),
                specification_provider=result.draft.output_provider,
                # A public interface is separately typed by ``provides``.  Sending
                # the same bytes again as a local specification would duplicate an
                # authority segment and make the bounded generation context
                # ambiguous (as well as needlessly consuming model context).
                specification_roots=tuple(
                    item.path
                    for item in promoted_artifacts
                    if item.path not in public_interface_paths
                ),
                authoring_inputs=(
                    _authoring_selector(implementation_skill_ref),
                    _authoring_selector(skill_ref),
                    _authoring_selector(planning_skill_ref),
                ),
                workflow_definition=_authoring_selector(workflow_ref),
                routing_policy=_authoring_selector(routing_ref),
                flavor_slots=tuple(
                    sorted(
                        (
                            *(
                                FlavorSlot.from_dict(
                                    {
                                        "slot_id": re.sub(
                                            r"[^a-z0-9-]+", "-", axis.lower()
                                        ).strip("-"),
                                        "axis": axis,
                                        "cardinality": (
                                            "exactly-one"
                                            if axis
                                            == "implementation.language-ecosystem"
                                            else "zero-or-one"
                                        ),
                                        "capability_contract": capabilities[0],
                                        "minimum": None,
                                        "maximum": None,
                                    }
                                )
                                for axis in build_needs
                                if axis != "build.system"
                            ),
                            *(
                                (
                                    FlavorSlot.from_dict(
                                        {
                                            "slot_id": "platform-os",
                                            "axis": "platform.os",
                                            "cardinality": "exactly-one",
                                            "capability_contract": capabilities[0],
                                            "minimum": None,
                                            "maximum": None,
                                        }
                                    ),
                                )
                                if "platform.os" not in build_needs
                                else ()
                            ),
                            FlavorSlot.from_dict(
                                {
                                    "slot_id": "build-system",
                                    "axis": "build.system",
                                    "cardinality": "zero-or-one",
                                    "capability_contract": capabilities[0],
                                    "minimum": None,
                                    "maximum": None,
                                }
                            ),
                        ),
                        key=lambda item: item.slot_id,
                    )
                ),
                entrypoints=entrypoints,
                acceptance_contracts=(
                    tuple(
                        _authoring_selector(reference)
                        for reference in root_acceptance_contracts
                    )
                    if node.coordinate == graph.root_coordinate
                    else ()
                ),
                source_dependencies=(),
            )
            authoring_path = component_root / "component.md"
            authoring_path.write_text(
                render_component_markdown(
                    authoring,
                    authoring_path,
                    project_root=staging,
                ),
                encoding="utf-8",
                newline="\n",
            )
            authored_components.append(authoring.identity.uri)

        created_flavors: list[str] = []
        created_flavor_axes: dict[str, str] = {}
        for proposal in proposals:
            raw_flavor = proposal.get("flavor_id")
            if not isinstance(raw_flavor, str):
                continue
            flavor = re.sub(r"[^a-z0-9._-]+", "-", raw_flavor.lower()).strip("-._")
            if not flavor:
                continue
            try:
                directory = flavor_selector_directory(f"+{flavor}")
            except ValueError:
                directory = flavor
            if directory in created_flavor_axes:
                continue
            created_flavors.append(flavor)
            axis = str(proposal.get("axis", "implementation.language-ecosystem"))
            created_flavor_axes[directory] = axis
            flavor_root = staging / "flavors" / directory
            flavor_path = flavor_root / "flavor.md"
            spec_path = flavor_root / "openspec" / "spec.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_content = _flavor_markdown(proposal).encode("utf-8")
            spec_path.write_bytes(spec_content)
            flavor_authoring = {
                "schema": FLAVOR_MARKDOWN_SCHEMA,
                "namespace": "source-derived",
                "name": flavor,
                "version": "1.0.0",
                "display_name": f"Source-derived {flavor.title()} Flavor",
                "primary_axis": axis,
                "target": flavor,
                "secondary_constraints": [],
                "applicable_capabilities": list(graph_capabilities),
                "provides": [
                    {
                        "name": f"flavor.{flavor}",
                        "version": "1.0.0",
                        "contract": None,
                    }
                ],
                "requires": [],
                "specification_roots": ["openspec/spec.md"],
                "authoring_inputs": [],
                "contributions": [],
                "conflicts": [
                    f"flavor://source-derived/{other}"
                    for other in sorted(
                        value
                        for value in {
                            str(item.get("flavor_id", "")) for item in proposals
                        }
                        if value
                        and value != raw_flavor
                        and str(
                            next(
                                (
                                    item.get("axis")
                                    for item in proposals
                                    if item.get("flavor_id") == value
                                ),
                                "",
                            )
                        )
                        == axis
                    )
                ],
                "co_requisites": [],
                "order_before": [],
                "order_after": [],
            }
            if flavor_path.is_file():
                existing, _description = parse_authoring_markdown(
                    flavor_path.read_bytes(), source=flavor_path.as_posix()
                )
                if (
                    existing.get("schema") != FLAVOR_MARKDOWN_SCHEMA
                    or existing.get("primary_axis") != axis
                    or existing.get("target") != flavor
                ):
                    raise CliFailure(
                        "promotion.flavor_collision",
                        f"source-derived Flavor {flavor!r} conflicts with initialized "
                        "Flavor authority",
                    )
                flavor_authoring = dict(existing)
                flavor_authoring["applicable_capabilities"] = list(graph_capabilities)
            flavor_path.write_bytes(
                render_authoring_markdown(
                    flavor_authoring,
                    f"# Source-derived {flavor.title()} Flavor\n\n"
                    "This reviewed Flavor captures target-specific policy recovered "
                    "from the source tree. Its referenced specification is the "
                    "generation authority.",
                )
            )
        if not created_flavors or "implementation.language-ecosystem" not in axes:
            raise CliFailure(
                "promotion.language_flavor_missing",
                "a complete project requires at least one reviewed language "
                "Flavor draft",
            )
        if len(authored_components) != len(node_coordinates):
            raise CliFailure(
                "promotion.component_authoring_failed",
                "promoted project authoring report is incomplete",
            )

        selected_qualification_flavors = tuple(
            canonical_flavor_selector(selector) for selector in qualification_flavors
        )
        if not selected_qualification_flavors:
            by_axis: dict[str, list[str]] = {}
            for flavor, axis in created_flavor_axes.items():
                by_axis.setdefault(axis, []).append(flavor)
            ambiguous_axes = tuple(
                axis for axis, values in sorted(by_axis.items()) if len(values) != 1
            )
            if ambiguous_axes:
                raise CliFailure(
                    "promotion.qualification_flavor_required",
                    "promoted project has multiple reviewed Flavors on axis "
                    f"{ambiguous_axes[0]!r}; select the qualification Flavor "
                    "explicitly",
                )
            selected_qualification_flavors = tuple(
                f"+{values[0]}" for _axis, values in sorted(by_axis.items())
            )

        selected_axes = set()
        selected_aliases = set()
        for selector in selected_qualification_flavors:
            if not selector.startswith("+"):
                continue
            selected_name = flavor_selector_directory(selector)
            selected_aliases.add(canonical_flavor_selector(selector)[1:])
            selected_path = staging / "flavors" / selected_name / "flavor.md"
            if not selected_path.is_file():
                continue
            selected_authoring, _description = parse_authoring_markdown(
                selected_path.read_bytes(), source=selected_path.as_posix()
            )
            selected_axis = selected_authoring.get("primary_axis")
            if isinstance(selected_axis, str):
                selected_axes.add(selected_axis)
        if (
            qualification_target == "host"
            and "platform.os" not in selected_axes
            and not selected_aliases.intersection(
                {
                    "flavor://literate-ai/os-linux",
                    "flavor://literate-ai/os-macos",
                    "flavor://literate-ai/os-windows",
                }
            )
        ):
            platform_flavor = (
                "windows"
                if sys.platform == "win32"
                else "macos"
                if sys.platform == "darwin"
                else "linux"
            )
            selected_qualification_flavors = (
                *selected_qualification_flavors,
                f"+flavor://literate-ai/os-{platform_flavor}",
            )

        # A promoted project owns its defaults; template host defaults describe the
        # machine that performed promotion, not the reviewed source target.  Fold
        # requested additions/removals into a final positive default set once, then
        # let every later lock/generate/qualify command consume those defaults
        # without replaying selectors and tripping the strict no-op guard.
        promoted_defaults = {"+flavor://literate-ai/build-bazel"}
        for selector in selected_qualification_flavors:
            operation = selector[:1]
            value = selector[1:]
            alias = canonical_flavor_selector("+" + value)[1:]
            if operation == "+":
                promoted_defaults.add(selector)
            elif operation == "-":
                promoted_defaults = {
                    item
                    for item in promoted_defaults
                    if canonical_flavor_selector("+" + item[1:])[1:] != alias
                }
        # Canonical initialization scopes operational Flavors to its tutorial
        # capability.  Promotion replaces that application graph, so every selected
        # operational Flavor must be rebound to the recovered public capabilities
        # before the lock resolver performs applicability filtering.
        for selector in sorted(promoted_defaults):
            if not selector.startswith("+"):
                continue
            flavor = flavor_selector_directory(selector)
            flavor_path = staging / "flavors" / flavor / "flavor.md"
            if not flavor_path.is_file():
                continue
            selected_authoring, selected_description = parse_authoring_markdown(
                flavor_path.read_bytes(), source=flavor_path.as_posix()
            )
            selected_authoring = dict(selected_authoring)
            selected_authoring["applicable_capabilities"] = list(graph_capabilities)
            flavor_path.write_bytes(
                render_authoring_markdown(
                    selected_authoring,
                    selected_description,
                )
            )
        project_store = ProjectConfigurationStore(staging)
        project_snapshot = project_store.read()
        project_store.update(
            project_snapshot,
            replace(
                project_snapshot.definition,
                default_flavor_selectors=tuple(sorted(promoted_defaults)),
            ),
        )

        from .component_locks import component_lock_from_args

        component_root = staging / "components" / name
        lock_report, lock_status = component_lock_from_args(
            Namespace(
                component=str(component_root),
                target=qualification_target,
                flavor=[],
                flavor_root=[],
                check=False,
                diff=False,
            )
        )
        if lock_status != 0:
            raise CliFailure(
                "promotion.component_lock_failed",
                "promoted project did not create its exact qualification lock",
            )
        # The root Component's lock is verified above and carries this
        # promotion's lifecycle authority.  A recovered Component graph can
        # promote more than one Component (one directory per graph node), and
        # every one of them was just materialized with its own
        # specification_roots pointing at files copied into its own
        # ``components/<name>/`` tree.  Without resolving each of their locks
        # here too, a broken specification_roots reference for a *non-root*
        # Component would only surface later -- as ``litai lock`` failing
        # with ``component_lock.content_unavailable`` well after promotion
        # already reported success.  Fail promotion instead of letting that
        # gap reach the promoted project.
        for other_coordinate, (_other_namespace, other_name) in sorted(
            node_coordinates.items()
        ):
            if other_coordinate == graph.root_coordinate:
                continue
            other_component_root = staging / "components" / other_name
            _other_report, other_status = component_lock_from_args(
                Namespace(
                    component=str(other_component_root),
                    target=qualification_target,
                    flavor=[],
                    flavor_root=[],
                    check=False,
                    diff=False,
                )
            )
            if other_status != 0:
                raise CliFailure(
                    "promotion.component_lock_failed",
                    "promoted project did not create its exact qualification "
                    f"lock for {other_coordinate}",
                )
        lock_identity = lock_report.get("component_lock_identity")
        if not isinstance(lock_identity, str):
            raise CliFailure(
                "promotion.component_lock_failed",
                "promoted project lock report omitted its exact identity",
            )
        from literate_ai.adapters.locked_generation_authority import (
            FilesystemLockedGenerationAuthorityReader,
            LockedGenerationAuthorityReaderError,
        )

        try:
            locked_snapshot = FilesystemLockedGenerationAuthorityReader().read(
                component_root,
                target_name=qualification_target,
                flavor_selectors=(),
            )
        except LockedGenerationAuthorityReaderError as exc:
            raise CliFailure(exc.code, exc.message) from exc
        locked_authority = locked_snapshot.authority
        if locked_authority.lock.identity.uri != lock_identity:
            raise CliFailure(
                "promotion.component_lock_changed",
                "promoted project lock changed before lifecycle authority creation",
            )
        from literate_ai.adapters.authority import FileAuthorityProjectionStore
        from literate_ai.authority import ComponentAuthorityLifecycle
        from literate_ai.source_to_specification.promotion_materialization import (
            SOURCE_PROMOTION_PROVENANCE_SCHEMA,
            PromotionInputKind,
        )

        forward_paths: dict[str, PromotionInputKind] = {
            "literate.project.json": PromotionInputKind.PROJECT_CONFIGURATION,
            "workflows/production/staging/dev/workflow.md": (
                PromotionInputKind.WORKFLOW
            ),
            "routing/production/staging/dev/routing.json": (
                PromotionInputKind.ROUTING_POLICY
            ),
            (
                "skills/specification-to-source/portable-application/SKILL.md"
            ): PromotionInputKind.FORWARD_SKILL,
            (
                "skills/specification-to-source/bazel-build-system/SKILL.md"
            ): PromotionInputKind.FORWARD_SKILL,
        }
        for coordinate, (_node_namespace, node_name) in node_coordinates.items():
            forward_paths[f"components/{node_name}/component.md"] = (
                PromotionInputKind.COMPONENT_INTENT
            )
            for artifact in component_specifications[coordinate]:
                forward_paths[f"components/{node_name}/{artifact.path}"] = (
                    PromotionInputKind.SPECIFICATION
                )
        if profile_content is not None:
            forward_paths[f"components/{name}/qualification/profile.json"] = (
                PromotionInputKind.PROJECT_CONFIGURATION
            )
        for selector in promoted_defaults:
            if not selector.startswith("+"):
                continue
            flavor = flavor_selector_directory(selector)
            flavor_root = staging / "flavors" / flavor
            if not flavor_root.is_dir() or flavor_root.is_symlink():
                raise CliFailure(
                    "promotion.flavor_missing",
                    f"selected qualification Flavor is unavailable: {flavor}",
                )
            for flavor_input in sorted(flavor_root.rglob("*")):
                if flavor_input.is_symlink() or not flavor_input.is_file():
                    continue
                relative = flavor_input.relative_to(staging).as_posix()
                forward_paths[relative] = PromotionInputKind.REVIEWED_FLAVOR
        forward_inputs = tuple(
            SourcePromotionInput(
                kind=kind,
                source_root=staging,
                source_root_label="promoted-project",
                source_path=path,
                target_path=path,
                expected_content_identity=(
                    "sha256:"
                    + hashlib.sha256((staging / path).read_bytes()).hexdigest()
                ),
            )
            for path, kind in sorted(forward_paths.items())
        )
        complete_audit = materializer.audit(forward_inputs)
        promotion_audits.append((complete_audit.identity, complete_audit))

        promotion_record = {
            "schema": SOURCE_PROMOTION_PROVENANCE_SCHEMA,
            "source_snapshot_identity": result.request.source_snapshot_id,
            "specification_set_identity": specification_set_id,
            "review_identity": review_id,
            "component_graph_identity": graph.identity,
            "translation_identity": (
                None if translation is None else canonical_digest(translation)
            ),
            "inverse_evidence_reference": (
                None
                if inverse_evidence_custody is None
                else {
                    "kind": "inverse-evidence-custody",
                    "identity": canonical_digest(inverse_evidence_custody),
                    "path": "inverse-evidence.json",
                }
            ),
            "generation_input_audit_references": [
                {
                    "kind": "generation-input-audit",
                    "identity": audit.identity,
                    "path": (
                        "generation-input-audits/"
                        f"{ContentIdentity.parse_uri(audit.identity).digest}.json"
                    ),
                }
                for _name, audit in promotion_audits
            ],
        }
        promotion_identity = canonical_identity(promotion_record)
        promotion_root = (
            staging / "provenance" / "source-promotion" / promotion_identity.digest
        )
        audit_root = promotion_root / "generation-input-audits"
        audit_root.mkdir(parents=True)
        (promotion_root / "reference.json").write_text(
            _json_text(promotion_record), encoding="utf-8", newline="\n"
        )
        if translation is not None:
            (promotion_root / "source-translation.json").write_text(
                _json_text(translation), encoding="utf-8", newline="\n"
            )
        if inverse_evidence_custody is not None:
            (promotion_root / "inverse-evidence.json").write_text(
                _json_text(inverse_evidence_custody), encoding="utf-8", newline="\n"
            )
        for _audit_name, audit in promotion_audits:
            audit_digest = ContentIdentity.parse_uri(audit.identity).digest
            (audit_root / f"{audit_digest}.json").write_text(
                _json_text(audit.to_dict()), encoding="utf-8", newline="\n"
            )

        authority_store = FileAuthorityProjectionStore(staging)
        source_identity = ContentIdentity.parse_uri(result.request.source_snapshot_id)
        specification_identity = ContentIdentity.parse_uri(specification_set_id)
        review_identity = ContentIdentity.parse_uri(review_id)
        locked_nodes = {
            node.revision.coordinate.uri: node for node in locked_authority.lock.nodes
        }
        for coordinate, (_node_namespace, _node_name) in node_coordinates.items():
            component_coordinate = f"component://{coordinate}"
            locked_node = locked_nodes.get(component_coordinate)
            if locked_node is None:
                raise CliFailure(
                    "promotion.component_lock_incomplete",
                    f"qualification lock omits promoted Component {coordinate}",
                )
            revision_identity = locked_node.revision.identity
            component_revisions[coordinate] = revision_identity
            inventory_projection = ComponentAuthorityLifecycle.inventory(
                component_coordinate=component_coordinate,
                source_snapshot_identity=source_identity,
                provenance_reference_identity=promotion_identity,
                evidence_identities=(source_identity,),
            )
            assisted_projection = ComponentAuthorityLifecycle.derive(
                inventory_projection,
                component_revision_identity=revision_identity,
                specification_set_identity=(
                    locked_node.revision.specification_set_identity
                ),
                evidence_identities=(specification_identity,),
            )
            retained_projection = ComponentAuthorityLifecycle.accept(
                assisted_projection,
                evidence_identities=(review_identity,),
            )
            for projection in (
                inventory_projection,
                assisted_projection,
                retained_projection,
            ):
                authority_store.append(projection)
        from .project import (
            documentation_review_from_args,
            validate_project_from_args,
        )

        authority_review = documentation_review_from_args(Namespace(path=str(staging)))
        review_document = authority_review.get("document")
        expected_marker = authority_review.get("expected_marker")
        if not isinstance(review_document, str) or not isinstance(expected_marker, str):
            raise CliFailure(
                "promotion.authority_review_invalid",
                "promoted project did not retain one documentation review marker",
            )
        review_path = staging.joinpath(*Path(review_document).parts)
        review_content = review_path.read_text(encoding="utf-8")
        updated_content, marker_count = re.subn(
            r"<!--\s*literate-ai:authority-reviewed sha256:[0-9a-f]{64}\s*-->",
            expected_marker,
            review_content,
        )
        if marker_count != 1:
            raise CliFailure(
                "promotion.authority_review_invalid",
                "promoted project did not retain one documentation review marker",
            )
        review_path.write_text(updated_content, encoding="utf-8", newline="\n")

        validate_project_from_args(Namespace(path=str(staging)))
        os.replace(staging, project_target)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return {
        "project_target": str(project_target),
        "component": f"components/{name}/component.md",
        "components": sorted(
            f"components/{node_name}/component.md"
            for _node_namespace, node_name in node_coordinates.values()
        ),
        "component_authoring_count": len(authored_components),
        "component_lock_identity": lock_identity,
        "qualification_target": qualification_target,
        "qualification_flavors": list(selected_qualification_flavors),
        "component_graph_identity": graph.identity,
        "authority_state": ComponentAuthorityState.DERIVED_SOURCE_RETAINED.value,
        "authority_projection_count": 3 * len(component_revisions),
        "generation_input_audit_count": len(promotion_audits),
        "proposed_flavors": sorted(created_flavors),
    }


def _diff(left: dict[str, str], right: dict[str, str]) -> dict[str, list[str]]:
    left_paths = set(left)
    right_paths = set(right)
    return {
        "added": sorted(right_paths - left_paths),
        "removed": sorted(left_paths - right_paths),
        "changed": sorted(
            path for path in left_paths & right_paths if left[path] != right[path]
        ),
        "unchanged": sorted(
            path for path in left_paths & right_paths if left[path] == right[path]
        ),
    }


def _authority_status_from_args(args: Namespace) -> dict[str, object]:
    """Report recorded and presently effective Component authority."""

    from literate_ai.adapters.authority import (
        AuthorityProjectionStoreError,
        FileAuthorityProjectionStore,
    )
    from literate_ai.adapters.locked_generation_authority import (
        FilesystemLockedGenerationAuthorityReader,
        LockedGenerationAuthorityReaderError,
    )
    from literate_ai.authority import ComponentAuthorityLifecycle
    from literate_ai.source_to_specification.promotion_materialization import (
        SourcePromotionError,
        verify_source_promotion_evidence,
    )

    requested = Path(args.project).resolve(strict=True)
    project = discover_project(requested)
    if project is None:
        raise CliFailure(
            "authority.project_required",
            "authority status requires a canonical Literate AI project",
        )
    revisions: dict[str, ContentIdentity] = {}
    authoring_identities: dict[str, ContentIdentity] = {}
    locked_snapshots = []
    components_root = project.root / "components"
    for lock_path in sorted(components_root.rglob("component.lock.json")):
        if lock_path.is_symlink() or not lock_path.is_file():
            raise CliFailure(
                "authority.component_path_unsafe",
                "Component authority status encountered an unsafe lock",
            )
        target_name = _read_json(lock_path).get("target_name")
        if not isinstance(target_name, str) or not target_name:
            raise CliFailure(
                "authority.component_lock_invalid",
                "Component authority status encountered a lock without a target",
            )
        try:
            locked = FilesystemLockedGenerationAuthorityReader().read(
                lock_path.parent,
                target_name=target_name,
            )
        except LockedGenerationAuthorityReaderError as exc:
            raise CliFailure(exc.code, exc.message) from exc
        locked_snapshots.append(locked)
        for node in locked.authority.lock.nodes:
            coordinate = node.revision.coordinate.uri
            previous = revisions.setdefault(coordinate, node.revision.identity)
            if previous != node.revision.identity:
                raise CliFailure(
                    "authority.component_lock_conflict",
                    f"current locks disagree for {coordinate}",
                )
            previous_authoring = authoring_identities.setdefault(
                coordinate, node.revision.authoring_identity
            )
            if previous_authoring != node.revision.authoring_identity:
                raise CliFailure(
                    "authority.component_lock_conflict",
                    f"current locks disagree on authored content for {coordinate}",
                )
    for manifest in sorted(components_root.rglob("component.md")):
        if manifest.is_symlink() or not manifest.is_file():
            raise CliFailure(
                "authority.component_path_unsafe",
                "Component authority status encountered an unsafe manifest",
            )
        authoring = parse_component_markdown(
            manifest,
            manifest.read_text(encoding="utf-8"),
            project_root=project.root,
        )
        expected_authoring_identity = authoring_identities.get(authoring.coordinate.uri)
        if expected_authoring_identity is None:
            raise CliFailure(
                "authority.component_lock_missing",
                "Component authority status requires an exact current lock for every "
                "authored Component",
            )
        if authoring.identity != expected_authoring_identity:
            raise CliFailure(
                "component_lock.stale",
                "Component authority status found authored content that differs from "
                "its exact current lock",
            )
    try:
        for locked in locked_snapshots:
            locked.require_unchanged()
    except LockedGenerationAuthorityReaderError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    try:
        store = FileAuthorityProjectionStore(project.root)
        coordinates = store.coordinates()
        if args.component is not None:
            selected = args.component
            if not selected.startswith("component://"):
                selected = f"component://{selected}"
            coordinates = tuple(item for item in coordinates if item == selected)
            if not coordinates:
                raise CliFailure(
                    "authority.component_unknown",
                    "requested Component has no authority projection",
                )
        reports = []
        for coordinate in coordinates:
            projection = store.current(coordinate)
            history = store.history(coordinate)
            try:
                verified = verify_source_promotion_evidence(project.root, projection)
                status = ComponentAuthorityLifecycle.status(
                    projection,
                    component_revision_identity=revisions.get(coordinate),
                    target_lock_identity=verified.target_lock_identity,
                    generation_closure=verified.generation_closure,
                    verifier_identity=verified.verifier_identity,
                    policy_identity=verified.policy_identity,
                )
                status_value = status.to_dict()
            except SourcePromotionError:
                status = ComponentAuthorityLifecycle.status(
                    projection,
                    component_revision_identity=revisions.get(coordinate),
                )
                status_value = status.to_dict()
                status_value["effective_state"] = (
                    ComponentAuthorityState.SOURCE_AUTHORITATIVE.value
                )
                status_value["blockers"] = [
                    *status_value["blockers"],
                    "promotion-evidence-invalid",
                ]
            reports.append(
                {
                    "component_coordinate": coordinate,
                    "projection_identity": projection.identity.uri,
                    "history": [item.identity.uri for item in history],
                    **status_value,
                }
            )
    except AuthorityProjectionStoreError as exc:
        raise CliFailure("authority.store_invalid", str(exc)) from exc
    return {
        "schema": "literate-ai/component-authority-status@1",
        "project": str(project.root),
        "components": reports,
        "summary": {
            state.value: sum(item["effective_state"] == state.value for item in reports)
            for state in ComponentAuthorityState
        },
    }


def _summary(
    bundle: dict[str, Any], result: SourceToSpecificationResult
) -> dict[str, Any]:
    return {
        "claim_kinds": {
            item.observation_id: item.claim_kind.value for item in result.observations
        },
        "coverage": {
            item.surface_id: item.state.value for item in result.coverage.entries
        },
        "uncertainty_ids": [item.uncertainty_id for item in result.uncertainty.items],
        "statement_ids": [item.statement_id for item in result.draft.statements],
        "flavor_ids": [item["flavor_id"] for item in bundle["flavor_drafts"]],
    }


def _conformance(
    descriptor: Path,
    case: dict[str, Any],
    *,
    skills_root: str | None,
) -> dict[str, Any]:
    previous_case = case.get("previous_case")
    invalidated_surfaces: list[str] = []
    if previous_case is None:
        bundle, result, _digests = _derive(
            descriptor,
            case,
            mode=RunMode.BOOTSTRAP,
            previous_specification_set_id=None,
            skills_root=skills_root,
        )
    else:
        if not isinstance(previous_case, str):
            raise CliFailure("cli.invalid_case", "previous_case must be a string")
        previous_descriptor, previous_data = _load_case(
            descriptor.parent / previous_case
        )
        previous_bundle, previous_result, _previous_digests = _derive(
            previous_descriptor,
            previous_data,
            mode=RunMode.AUDIT,
            previous_specification_set_id=f"sample-base:{case['case_id']}",
            skills_root=skills_root,
        )
        del previous_bundle
        bundle, result, digests = _derive(
            descriptor,
            case,
            mode=RunMode.REFRESH,
            previous_specification_set_id=f"sample-base:{case['case_id']}",
            skills_root=skills_root,
        )
        catalog = _load_discovered_skill_catalog(descriptor, skills_root)
        selected = builtin_skill_set(catalog)
        invalidation = plan_refresh(
            previous_result,
            result.request,
            previous_skill_set=selected,
            new_skill_set=selected,
            current_evidence_digests=digests,
            changed_source_paths=_strings(
                case.get("changed_source_paths", []), "changed_source_paths"
            ),
        )
        invalidated_surfaces = list(invalidation.invalidated_surface_ids)
    actual = _summary(bundle, result)
    actual["invalidated_surface_ids"] = invalidated_surfaces
    expected = _object(case["expected"], "expected")
    mismatches = [
        key
        for key in sorted(set(expected) | set(actual))
        if expected.get(key) != actual.get(key)
    ]
    return {
        "schema": CONFORMANCE_SCHEMA,
        "case_id": case["case_id"],
        "passed": not mismatches,
        "mismatches": mismatches,
        "expected": expected,
        "actual": actual,
    }


def _complete_standard_lifecycle_qualification(
    *,
    args: argparse.Namespace,
    project: Any,
    component_root: Path,
    source: Path,
    profile: LocalQualificationProfile,
    authority_store: Any,
    authority_projection: Any,
    locked_snapshot: Any,
    component_coordinate: str,
    specification_set_identity: ContentIdentity,
    source_snapshot_identity: ContentIdentity,
    component_audit: Any,
    flavor_set_identity: ContentIdentity,
    skill_set_identity: ContentIdentity,
    workflow_identity: ContentIdentity,
    routing_identity: ContentIdentity,
    output_path: Path,
) -> tuple[dict[str, Any], int]:
    """Run and admit only the filesystem Standard lifecycle qualification contract."""

    from literate_ai.adapters.generation_preparation import (
        FilesystemLockedGenerationApplicationAdapter,
        GenerationPreparationError,
    )
    from literate_ai.adapters.qualification import (
        FilesystemQualificationError,
        FilesystemStandardQualificationAdapter,
    )
    from literate_ai.adapters.standard_lifecycle_binding import (
        StandardLifecycleBindingError,
        resolve_standard_project_lifecycle_driver,
    )
    from literate_ai.application import GenerationPreparationRequest
    from literate_ai.application.component_generation_scheduling import (
        ComponentInvalidationDecision,
    )
    from literate_ai.application.source_promotion import (
        LockedSourcePromotionError,
        SourcePromotionService,
    )
    from literate_ai.contracts import (
        ComponentChangeSurface,
        StandardProjectLifecycleDriver,
    )
    from literate_ai.source_to_specification import (
        QualificationLifecyclePlan,
        VerifiedSourcePromotionEvidence,
    )

    driver = project.definition.lifecycle_driver
    if not isinstance(driver, StandardProjectLifecycleDriver):
        raise CliFailure(
            "qualification.standard_driver_required",
            "regenerative qualification requires the in-process Standard lifecycle",
        )
    try:
        binding = resolve_standard_project_lifecycle_driver(driver)
        prepared = FilesystemLockedGenerationApplicationAdapter().prepare(
            GenerationPreparationRequest(
                component_root=component_root,
                target_name=args.target,
                flavor_selectors=tuple(args.flavor),
                flavor_roots=project.roots("flavor"),
            )
        )
    except (GenerationPreparationError, StandardLifecycleBindingError) as exc:
        raise CliFailure(
            getattr(exc, "code", "qualification.standard_preparation_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc
    if (
        prepared.locked_authority_snapshot.authority.lock.identity
        != locked_snapshot.authority.lock.identity
    ):
        raise CliFailure(
            "qualification.component_lock_changed",
            "prepared Standard lifecycle differs from the accepted Component lock",
        )
    lock = prepared.locked_authority_snapshot.authority.lock
    revisions = tuple(item.revision.identity for item in lock.nodes)
    invalidation = ComponentInvalidationDecision(
        "litai-regenerative-qualification",
        lock.root_revision,
        ComponentChangeSurface.LOCAL_AUTHORITY,
        revisions,
        revisions,
        revisions,
    )
    owns_scratch = args.scratch_root is None
    scratch_root = (
        Path(tempfile.mkdtemp(prefix="litai-qualification-scratch-")).resolve(
            strict=True
        )
        if owns_scratch
        else Path(args.scratch_root).resolve(strict=True)
    )
    retained_store = getattr(args, "retained_evidence_store", None)
    retained_evidence = None
    try:
        if retained_store is not None:
            store_path = Path(retained_store).absolute()
            if store_path.resolve().is_relative_to(source):
                raise CliFailure(
                    "qualification.evidence_store_in_source",
                    "retained evidence storage must be outside the source baseline",
                )
            if store_path.resolve().is_relative_to(scratch_root):
                raise CliFailure(
                    "qualification.evidence_store_in_scratch",
                    "retained evidence storage must be outside qualification scratch",
                )
        adapter = FilesystemStandardQualificationAdapter(
            project=project,
            prepared=prepared,
            binding=binding,
            invalidation=invalidation,
            source_root=source,
            source_snapshot_identity=source_snapshot_identity,
            profile=profile,
            scratch_root=scratch_root,
            retain_library_products=retained_store is not None,
            pipeline_model=getattr(args, "model", None),
        )
        run_identities = tuple(
            sorted(
                (
                    canonical_identity(
                        {
                            "schema": "literate-ai/qualification-run@2",
                            "component_lock_identity": lock.identity.uri,
                            "specification_set_identity": (
                                specification_set_identity.uri
                            ),
                            "source_snapshot_identity": source_snapshot_identity.uri,
                            "profile_identity": profile.identity,
                            "ordinal": ordinal,
                        }
                    )
                    for ordinal in range(profile.minimum_clean_runs)
                ),
                key=lambda item: item.uri,
            )
        )
        plan = QualificationLifecyclePlan(
            lock.target_profile_identity,
            lock.identity,
            specification_set_identity,
            source_snapshot_identity,
            ContentIdentity.parse_uri(component_audit.identity),
            ContentIdentity.parse_uri(component_audit.materialized_tree_identity),
            run_identities,
            adapter.parity.case_map,
        )
        qualification = adapter.qualify(plan)
        if any(
            run.driver_identity != binding.driver.identity
            or run.lifecycle_policy_identity != binding.policy.identity
            or run.framework_distribution_identity != binding.distribution.identity
            for run in qualification.runs
        ):
            raise CliFailure(
                "qualification.standard_binding_mismatch",
                "qualification result differs from the resolved Standard binding",
            )
        if retained_store is not None:
            from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
            from literate_ai.adapters.qualification_archive import (
                encode_qualification_archive,
                reopen_qualification_archive,
            )
            from literate_ai.security.evidence.storage import EvidenceReadLimits

            # This publishes immutable transport bytes, not importer admission.
            # A failed later admission may leave an unreferenced CAS object, which
            # must not be deleted: another transaction may already reference it.
            limit = adapter.max_capture_bytes
            archive = encode_qualification_archive(
                adapter.evidence_blobs, max_bytes=limit, max_records=100_000
            )
            store = FileSystemEvidenceStore(
                store_path,
                writable=True,
                limits=EvidenceReadLimits(
                    maximum_blob_bytes=limit, maximum_total_bytes=limit
                ),
            )
            reference = store.put_bytes(archive, media_type="application/zip")
            reopened = reopen_qualification_archive(
                store.get_bytes(reference),
                reference,
                max_bytes=limit,
                max_records=100_000,
            )
            if reopened.read_json(qualification.identity) != qualification.to_dict():
                raise CliFailure(
                    "qualification.retained_result_mismatch",
                    "retained evidence does not contain the qualified lifecycle result",
                )
            adapter.lifecycle.require_current_authority()
            retained_evidence = {
                "archive": reference.to_dict(),
                "qualification_identity": qualification.identity.uri,
                "runs": [
                    {
                        "run_identity": capture.run.run_identity.uri,
                        "export_set_identity": capture.exports.identity.uri,
                    }
                    for capture in adapter.library_captures
                ],
            }
    except (FilesystemQualificationError, SourceToSpecificationError) as exc:
        raise CliFailure(
            getattr(exc, "code", "qualification.lifecycle_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc
    except CliFailure:
        raise
    except (OSError, ValueError) as exc:
        if retained_store is None:
            raise
        raise CliFailure(
            "qualification.retained_publication_failed",
            "retained qualification evidence could not be published and reopened",
        ) from exc
    finally:
        if owns_scratch:
            shutil.rmtree(scratch_root)

    try:
        adapter.lifecycle.require_current_authority()
    except FilesystemQualificationError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    locked_snapshot.require_unchanged()
    if inventory_source(source).identity != source_snapshot_identity.uri:
        raise CliFailure(
            "qualification.source_changed",
            "source baseline changed during lifecycle qualification",
        )
    try:
        current_projection = authority_store.current(component_coordinate)
    except Exception as exc:
        raise CliFailure(
            "qualification.authority_changed",
            "Component authority changed during lifecycle qualification",
        ) from exc
    if current_projection != authority_projection:
        raise CliFailure(
            "qualification.authority_changed",
            "Component authority changed during lifecycle qualification",
        )
    try:
        verified_promotion = SourcePromotionService().verify_evidence(
            project.root, current_projection
        )
    except (SourceToSpecificationError, ValueError) as exc:
        raise CliFailure(
            "qualification.promotion_evidence_invalid",
            "promotion evidence changed or became unavailable during qualification",
        ) from exc
    qualification_identity = qualification.identity
    closure = ComponentGenerationClosure(
        flavor_set_identity,
        skill_set_identity,
        workflow_identity,
        routing_identity,
        ContentIdentity.parse_uri(component_audit.identity),
        ContentIdentity.parse_uri(component_audit.materialized_tree_identity),
        qualification_identity,
    )
    verified = VerifiedSourcePromotionEvidence(
        verified_promotion.promotion_input_audits,
        authority_projection=current_projection,
        target_lock_identity=lock.identity,
        generation_closure=closure,
        verifier_identity=qualification.case_map.verifier_identity,
        policy_identity=qualification.runs[0].lifecycle_policy_identity,
        qualification_lifecycle_result=qualification,
        translation=verified_promotion.translation,
        translation_identity=verified_promotion.translation_identity,
        inverse_evidence_custody=verified_promotion.inverse_evidence_custody,
        inverse_evidence_custody_identity=(
            verified_promotion.inverse_evidence_custody_identity
        ),
    )
    qualification_root = project.root / "provenance" / "qualification"
    qualification_root.mkdir(parents=True, exist_ok=True)
    if qualification_root.is_symlink() or not qualification_root.is_dir():
        raise CliFailure(
            "qualification.evidence_path_unsafe",
            "qualification evidence root must be a direct directory",
        )
    qualification_path = qualification_root / f"{qualification_identity.digest}.json"
    qualification_bytes = _json_text(qualification.to_dict()).encode()
    if qualification_path.exists():
        if (
            qualification_path.is_symlink()
            or qualification_path.read_bytes() != qualification_bytes
        ):
            raise CliFailure(
                "qualification.evidence_conflict",
                "qualification evidence identity is occupied by different bytes",
            )
    else:
        _write_new_json(qualification_path, qualification.to_dict())
    try:
        authority_store.append_qualified(locked_snapshot.authority, verified)
        qualified_projection = authority_store.current(component_coordinate)
    except LockedSourcePromotionError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    record = {
        "schema": "literate-ai/local-regenerative-qualification@2",
        "component_coordinate": component_coordinate,
        "component_lock_identity": lock.identity.uri,
        "qualification_lifecycle_result": qualification.to_dict(),
    }
    if retained_evidence is not None:
        record["retained_evidence"] = retained_evidence
    _write_new_json(output_path, record)
    return {
        "schema": "literate-ai/specification-qualification@2",
        "qualified": True,
        "release_implementation_authority": "specification",
        "blockers": [],
        "decision_identity": qualification_identity.uri,
        "authority_projection_identity": qualified_projection.identity.uri,
        "component_lock_identity": lock.identity.uri,
        "record": str(output_path),
        **(
            {"retained_evidence": retained_evidence}
            if retained_evidence is not None
            else {}
        ),
    }, 0


def spec_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.spec_command == "accept" and getattr(args, "integrate_project", None):
        from literate_ai.adapters.lifecycle_lock import (
            ProjectLifecycleLockError,
            project_lifecycle_lock,
        )

        root = Path(args.integrate_project).resolve(strict=True)
        try:
            with project_lifecycle_lock(root, operation="spec.accept.integrate"):
                # Stage, evidence, paths and registry are revalidated under the lock.
                return _spec_from_args(args)
        except ProjectLifecycleLockError as exc:
            raise CliFailure(exc.code, exc.message) from exc
    return _spec_from_args(args)


def _spec_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    command = args.spec_command
    if command == "merge":
        from literate_ai.adapters.spec_merge import (
            SpecMergeError,
            apply_spec_merge,
            plan_spec_merge,
        )

        try:
            planned = plan_spec_merge(
                Path(args.project),
                Path(args.source),
                component=args.component,
            )
            if getattr(args, "apply", False):
                planned["applied"] = apply_spec_merge(planned)
                planned["mode"] = "applied"
            return planned, 0
        except SpecMergeError as exc:
            raise CliFailure(exc.code, exc.message) from exc
    if command in {"format", "validate", "explain", "scxml-review"}:
        from .specifications import (
            SpecificationCliError,
            scxml_review_from_args,
            specification_corpus_from_args,
        )

        try:
            if command == "scxml-review":
                return scxml_review_from_args(args), 0
            return specification_corpus_from_args(args)
        except SpecificationCliError as exc:
            raise CliFailure(exc.code, exc.message) from exc
    if command == "attest":
        source = Path(args.source).resolve()
        inventory = inventory_source(source)
        key = read_local_trust_key(args.key)
        attestation = sign_source_inventory(
            inventory.identity,
            signer=args.signer,
            machine=args.machine,
            key=key,
        )
        return attestation.to_dict(), 0
    if command == "skills":
        catalog = _load_discovered_skill_catalog(None, args.skills)
        selected = builtin_skill_set(catalog)
        return {
            "schema": "literate-ai/spec-authoring-skill-catalog@1",
            "skill_set": canonical_value(selected),
            "skills": [
                canonical_value(catalog[reference.skill_id])
                for reference in selected.skills
            ],
        }, 0
    if command == "status":
        return _authority_status_from_args(args), 0
    if command in {"derive", "audit", "accept", "refresh", "conformance"}:
        standalone = _standalone_source(args.case)
        if standalone is None:
            descriptor, selected_case = _load_case(args.case)
            standalone = _source_root(descriptor, selected_case)
        _require_source_to_specification_project_index(standalone)
    if command == "derive":
        standalone = _standalone_source(args.case)
        if standalone is not None:
            bundle, _result = _derive_standalone(
                standalone,
                skills_root=args.skills,
                attestation_path=args.attestation,
                trust_key_path=args.trust_key,
                translator_kind=args.translator,
                allow_model_egress=args.allow_model_egress,
                model=args.model,
                model_evidence_byte_budget=args.model_evidence_byte_budget,
            )
            return bundle, 0
        if args.translator != "static" or args.allow_model_egress or args.model:
            raise CliFailure(
                "cli.translator_case_unsupported",
                "case descriptors use their deterministic conformance executor",
            )
        descriptor, case = _load_case(args.case)
        bundle, _result, _digests = _derive(
            descriptor,
            case,
            mode=RunMode.BOOTSTRAP,
            previous_specification_set_id=None,
            skills_root=args.skills,
        )
        return bundle, 0
    if command == "audit":
        baseline_bundle, baseline = _unwrap_bundle(args.baseline)
        standalone = _standalone_source(args.case)
        if standalone is not None:
            bundle, _result = _derive_standalone(
                standalone,
                skills_root=args.skills,
                attestation_path=args.attestation,
                trust_key_path=args.trust_key,
                translator_kind=args.translator,
                allow_model_egress=args.allow_model_egress,
                model=args.model,
                model_evidence_byte_budget=args.model_evidence_byte_budget,
                mode=RunMode.AUDIT,
                previous_specification_set_id=baseline.draft.draft_id,
            )
        else:
            if args.translator != "static" or args.allow_model_egress or args.model:
                raise CliFailure(
                    "cli.translator_case_unsupported",
                    "case descriptors use their deterministic conformance executor",
                )
            descriptor, case = _load_case(args.case)
            bundle, _result, _digests = _derive(
                descriptor,
                case,
                mode=RunMode.AUDIT,
                previous_specification_set_id=baseline.draft.draft_id,
                skills_root=args.skills,
            )
        return {
            "schema": "literate-ai/source-to-specification-audit@1",
            "bundle": bundle,
            "artifact_diff": _diff(
                {
                    item["path"]: item["content"]
                    for item in baseline_bundle["result"]["draft"]["artifacts"]
                },
                {
                    item["path"]: item["content"]
                    for item in bundle["result"]["draft"]["artifacts"]
                },
            ),
        }, 0
    if command == "review":
        bundle, result = _unwrap_bundle(args.bundle)
        unknown_resolutions = set(args.resolve) - {
            item.uncertainty_id for item in result.uncertainty.items
        }
        if unknown_resolutions:
            raise CliFailure("cli.unknown_uncertainty", "review resolution is unknown")
        reviewed_graph = (
            None
            if args.component_graph is None
            else _read_reviewed_component_graph(args.component_graph)
        )
        if reviewed_graph is not None:
            try:
                validate_component_graph_projection(result, reviewed_graph)
            except SourceToSpecificationError as exc:
                raise CliFailure(exc.code, exc.message) from exc
            if (
                result.component_graph_draft is not None
                and result.component_graph_draft != reviewed_graph
            ):
                raise CliFailure(
                    "review.component_graph_mismatch",
                    "reviewed Component graph differs from the derived semantic graph",
                )
        reviewed_artifacts = result.draft.artifacts
        reviewed_validation = result.draft.validation
        if reviewed_graph is not None and result.component_graph_draft is None:
            reviewed_artifacts, reviewed_validation = _static_review_artifacts(
                result, reviewed_graph
            )
        review = SpecificationReviewDecision(
            review_id=canonical_digest(
                {
                    "draft_id": result.draft.draft_id,
                    "actor": args.actor,
                    "resolved": tuple(sorted(args.resolve)),
                    "component_graph_identity": (
                        None if reviewed_graph is None else reviewed_graph.identity
                    ),
                }
            ),
            draft_id=result.draft.draft_id,
            base_specification_set_id=result.request.previous_specification_set_id,
            actor=args.actor,
            authority=ReviewAuthority.HUMAN,
            reason=args.reason,
            statement_decisions=tuple(
                ReviewStatementDecision(
                    statement_id=item.statement_id,
                    disposition=ReviewDisposition.ACCEPT,
                    reason="accepted from exact source evidence",
                )
                for item in result.draft.statements
            ),
            resolved_uncertainty_ids=tuple(sorted(args.resolve)),
            resulting_artifacts=reviewed_artifacts,
            validation=reviewed_validation,
            component_graph_draft=reviewed_graph,
        )
        review_wire = review_to_wire(review)
        response = {
            "schema": "literate-ai/specification-review-decision@1",
            "review": review_wire,
        }
        if bundle.get("source_kind") == "standalone-local":
            if args.key is None:
                raise CliFailure(
                    "cli.review_key_required",
                    "standalone source review requires --key",
                )
            response["review_attestation"] = sign_review(
                review_wire,
                actor=review.actor,
                key=read_local_trust_key(args.key),
            )
        return response, 0
    if command == "accept":
        bundle, result = _unwrap_bundle(args.bundle)
        standalone = bundle.get("source_kind") == "standalone-local"
        if standalone:
            selected_source = Path(args.case).resolve()
            inventory = inventory_source(selected_source)
            source_root = (
                selected_source if selected_source.is_dir() else selected_source.parent
            )
            if result.request.source_snapshot_id != inventory.identity:
                raise CliFailure(
                    "cli.source_drift", "analyzed source changed after derivation"
                )
            gate = _object(bundle.get("review_gate"), "review gate")
            if not gate.get("promotion_eligible"):
                raise CliFailure(
                    "promotion.source_unverified",
                    "standalone source is not eligible for promotion",
                )
            if args.trust_key is None:
                raise CliFailure(
                    "cli.trust_key_required",
                    "standalone acceptance requires --trust-key",
                )
            key = read_local_trust_key(args.trust_key)
            security = _object(bundle.get("security"), "security metadata")
            attestation = parse_source_attestation(security.get("origin_attestation"))
            verify_source_attestation(
                attestation,
                expected_source_snapshot_id=inventory.identity,
                key=key,
            )
        else:
            descriptor, case = _load_case(args.case)
            source_root = _source_root(descriptor, case)
        target = Path(args.target).resolve()
        if _contained(source_root, target) or _contained(target, source_root):
            raise CliFailure(
                "cli.target_overlaps_source",
                "accept target must be separate from the analyzed source",
            )
        if target.exists() or target.is_symlink():
            raise CliFailure(
                "cli.target_exists", "accept target must not already exist"
            )
        project_target = (
            Path(args.project_target).resolve()
            if args.project_target is not None
            else None
        )
        integration_project = None
        if args.integrate_project is not None:
            from literate_ai.adapters.conversion_authority import (
                ConversionAuthorityError,
                FilesystemConversionAuthorityStore,
                require_native_project_path,
            )
            from literate_ai.contracts.operator_adoption import ConversionAuthorityStage

            integration_path = Path(args.integrate_project).resolve(strict=True)
            integration_project = discover_project(integration_path)
            if (
                integration_project is None
                or integration_project.root != integration_path
            ):
                raise CliFailure(
                    "promotion.integration_project_invalid",
                    "integration target must be an existing canonical project root",
                )
            try:
                integration_state = FilesystemConversionAuthorityStore(
                    integration_project.root
                ).load_optional()
            except ConversionAuthorityError as exc:
                raise CliFailure(exc.code, exc.message) from exc
            if (
                integration_state is None
                or integration_state.stage is not ConversionAuthorityStage.RETAINED
            ):
                raise CliFailure(
                    "promotion.integration_stage_invalid",
                    "native promotion integration requires retained conversion "
                    "authority",
                )
            graph = result.component_graph_draft
            draft = result.component_definition_draft
            coordinate = (
                graph.root_coordinate
                if graph is not None
                else draft.coordinate
                if draft is not None
                else "native/component"
            )
            namespace, name = _coordinate_parts(coordinate, "component")
            project_target = (
                integration_project.root
                / ".literate"
                / "native-projects"
                / f"{namespace}-{name}"
            )
            try:
                require_native_project_path(integration_project, project_target)
            except ConversionAuthorityError as exc:
                raise CliFailure(exc.code, exc.message) from exc
        if project_target is not None:
            if project_target == target or (
                integration_project is None
                and (
                    _contained(source_root, project_target)
                    or _contained(project_target, source_root)
                )
            ):
                raise CliFailure(
                    "promotion.project_target_overlap",
                    "promoted project must be separate from source and accepted set",
                )
            if project_target.exists() or project_target.is_symlink():
                raise CliFailure(
                    "promotion.project_target_exists",
                    "promoted project target must not already exist",
                )
        if not standalone and result.request.source_snapshot_id != _source_identity(
            source_root
        ):
            raise CliFailure(
                "cli.source_drift", "analyzed source changed after derivation"
            )
        review = _unwrap_review(args.review)
        if standalone:
            review_envelope = _read_json(args.review)
            if review_envelope.get("schema") == CLI_RESULT_SCHEMA:
                review_envelope = _object(
                    review_envelope.get("result"), "CLI review result"
                )
            verify_review_attestation(
                review_envelope.get("review_attestation"),
                review=_object(review_envelope.get("review"), "review"),
                actor=review.actor,
                key=key,
            )
        translation = bundle.get("translation")
        inverse_evidence_custody: dict[str, object] | None = None
        if translation is not None:
            translation_record = _object(translation, "translation record")
            translation_schema = translation_record.get("schema")
            if translation_schema not in {
                SOURCE_TRANSLATION_RUN_SCHEMA,
                PREVIOUS_SOURCE_TRANSLATION_RUN_SCHEMA,
                LEGACY_SEMANTIC_SOURCE_TRANSLATION_RUN_SCHEMA,
                LEGACY_SOURCE_TRANSLATION_RUN_SCHEMA,
            }:
                raise CliFailure(
                    "model_translation.schema_unsupported",
                    "translation record uses an unsupported schema",
                )
            translation_mode = translation_record.get("mode")
            semantic_mode = (
                (
                    translation_schema == SOURCE_TRANSLATION_RUN_SCHEMA
                    and translation_mode == MODEL_TRANSLATION_MODE
                )
                or (
                    translation_schema == PREVIOUS_SOURCE_TRANSLATION_RUN_SCHEMA
                    and translation_mode == MODEL_TRANSLATION_MODE
                )
                or (
                    translation_schema == LEGACY_SEMANTIC_SOURCE_TRANSLATION_RUN_SCHEMA
                    and translation_mode == MODEL_TRANSLATION_MODE
                )
                or (
                    translation_schema == LEGACY_SOURCE_TRANSLATION_RUN_SCHEMA
                    and translation_mode == LEGACY_MODEL_TRANSLATION_MODE
                )
            )
            if semantic_mode:
                if (
                    bundle.get("schema") == BUNDLE_SCHEMA
                    and bundle.get("behavioral_surface_inventory") is None
                ):
                    raise CliFailure(
                        "surface_inventory.missing",
                        "current semantic result bundle omits its behavioral inventory",
                    )
                if (
                    bundle.get("schema") == BUNDLE_SCHEMA
                    and bundle.get("evidence_partition_manifest") is None
                ):
                    raise CliFailure(
                        "evidence_partition.missing",
                        "current semantic result bundle omits its evidence "
                        "partition manifest",
                    )
                if (
                    bundle.get("schema") == BUNDLE_SCHEMA
                    and bundle.get("evidence_batch_plan") is None
                ):
                    raise CliFailure(
                        "evidence_batch.missing",
                        "current semantic result bundle omits its evidence batch plan",
                    )
                expected_flavors = validate_model_translation_record(
                    value=translation_record,
                    result=result,
                    skill_catalog=load_builtin_skill_catalog(),
                    surface_inventory=bundle.get("behavioral_surface_inventory"),
                    evidence_partition_manifest=bundle.get(
                        "evidence_partition_manifest"
                    ),
                    evidence_batch_plan=bundle.get("evidence_batch_plan"),
                    source_inventory=bundle.get("source_inventory"),
                )
                if canonical_value(bundle.get("flavor_drafts")) != canonical_value(
                    expected_flavors
                ):
                    raise CliFailure(
                        "model_translation.flavor_binding_mismatch",
                        "Flavor drafts do not match the verified model responses",
                    )
                if translation_schema == SOURCE_TRANSLATION_RUN_SCHEMA:
                    inverse_evidence_custody = {
                        "schema": INVERSE_EVIDENCE_CUSTODY_SCHEMA,
                        "translation_identity": canonical_digest(translation_record),
                        "source_inventory": bundle.get("source_inventory"),
                        "behavioral_surface_inventory": bundle.get(
                            "behavioral_surface_inventory"
                        ),
                        "evidence_partition_manifest": bundle.get(
                            "evidence_partition_manifest"
                        ),
                        "evidence_batch_plan": bundle.get("evidence_batch_plan"),
                    }
                    validate_inverse_evidence_custody(
                        value=inverse_evidence_custody,
                        translation=translation_record,
                    )
            elif translation_mode == "deterministic-static":
                if (
                    translation_record.get("intelligence") is not None
                    or translation_record.get("journals") != []
                    or set(translation_record)
                    != {"schema", "mode", "intelligence", "journals"}
                    or any(
                        item.model_call_id is not None for item in result.observations
                    )
                ):
                    raise CliFailure(
                        "model_translation.record_invalid",
                        "static translation record contains model provenance",
                    )
            else:
                raise CliFailure(
                    "model_translation.record_invalid",
                    "translation record mode is unsupported",
                )
        effective_graph = review.component_graph_draft or result.component_graph_draft
        if review.component_graph_draft is not None:
            try:
                validate_component_graph_projection(
                    result, review.component_graph_draft
                )
            except SourceToSpecificationError as exc:
                raise CliFailure(exc.code, exc.message) from exc
        result = replace(
            result,
            component_graph_draft=effective_graph,
            draft=replace(
                result.draft,
                artifacts=review.resulting_artifacts,
                validation=review.validation,
            ),
        )
        specification = promote(
            result,
            review,
            PromotionPolicy(
                "signed-local-human-reviewed@1" if standalone else "human-reviewed@1"
            ),
            expected_base_specification_set_id=result.request.previous_specification_set_id,
        )
        guard = SourceTreeFingerprint(source_root)
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
        try:
            for artifact in specification.artifacts:
                artifact_target = staging / artifact.path
                artifact_target.parent.mkdir(parents=True, exist_ok=True)
                artifact_target.write_bytes(artifact.content.encode("utf-8"))
            manifest = {
                "schema": "literate-ai/accepted-specification-set@1",
                "specification_set_id": specification.specification_set_id,
                "source_snapshot_id": specification.source_snapshot_id,
                "authority_scope": "intent",
                "release_implementation_authority_at_acceptance": "source-baseline",
                "provider": specification.provider,
                "artifacts": canonical_value(specification.artifacts),
                "statements": canonical_value(specification.statements),
                "source_draft_id": specification.source_draft_id,
                "review_id": specification.review_id,
                "base_specification_set_id": specification.base_specification_set_id,
                "promotion_policy_id": specification.promotion_policy_id,
            }
            (staging / "specification-set.json").write_text(_json_text(manifest))
            (staging / "review.json").write_text(
                _json_text(
                    {
                        "schema": "literate-ai/specification-review-decision@1",
                        "review": review_to_wire(review),
                    }
                )
            )
            if translation is not None:
                translation_identity = ContentIdentity.parse_uri(
                    canonical_digest(translation)
                )
                journal_target = (
                    staging
                    / "provenance"
                    / "source-promotion"
                    / translation_identity.digest
                    / "source-translation.json"
                )
                journal_target.parent.mkdir(parents=True, exist_ok=True)
                journal_target.write_text(_json_text(translation), encoding="utf-8")
            flavor_drafts = bundle.get("flavor_drafts")
            if isinstance(flavor_drafts, list):
                (staging / "flavor-drafts.json").write_text(
                    _json_text(
                        {
                            "schema": "literate-ai/accepted-flavor-drafts@1",
                            "flavor_drafts": flavor_drafts,
                        }
                    ),
                    encoding="utf-8",
                )
            if result.component_definition_draft is not None:
                (staging / "component-definition-draft.json").write_text(
                    _json_text(canonical_value(result.component_definition_draft)),
                    encoding="utf-8",
                )
            if result.component_graph_draft is not None:
                (staging / "component-graph-draft.json").write_text(
                    _json_text(canonical_value(result.component_graph_draft)),
                    encoding="utf-8",
                )
            guard.require_unchanged()
            os.replace(staging, target)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        guard.require_unchanged()
        promoted_project = None
        if project_target is not None:
            try:
                parent_selection = None
                if integration_project is not None:
                    from literate_ai.adapters.repository_lineage import (
                        FilesystemRepositoryLineageStore,
                        RepositoryLineageStoreError,
                    )

                    try:
                        parent_selection, _ = FilesystemRepositoryLineageStore(
                            integration_project.root
                        ).load()
                    except RepositoryLineageStoreError as exc:
                        raise CliFailure(exc.code, exc.message) from exc
                promoted_project = _create_promoted_project(
                    accepted_root=target,
                    project_target=project_target,
                    result=result,
                    flavor_drafts=flavor_drafts,
                    project_id=args.project_id,
                    qualification_profile=(
                        Path(args.qualification_profile).resolve(strict=True)
                        if args.qualification_profile is not None
                        else None
                    ),
                    qualification_target=args.qualification_target,
                    qualification_flavors=tuple(args.flavor),
                    specification_set_id=specification.specification_set_id,
                    review_id=specification.review_id,
                    translation=translation,
                    inverse_evidence_custody=inverse_evidence_custody,
                    parent_selection=parent_selection,
                )
                if integration_project is not None:
                    from literate_ai.adapters.conversion_authority import (
                        ConversionAuthorityError,
                        register_native_project,
                    )

                    try:
                        register_native_project(integration_project, project_target)
                    except ConversionAuthorityError as exc:
                        raise CliFailure(exc.code, exc.message) from exc
                    promoted_project["integrated_project"] = str(
                        integration_project.root
                    )
            except BaseException:
                if target.exists():
                    shutil.rmtree(target)
                # The creator cleans its own staging tree on failure. A collision
                # must never let this caller delete someone else's installed child.
                if (
                    integration_project is not None
                    and promoted_project is not None
                    and project_target.exists()
                ):
                    shutil.rmtree(project_target)
                raise
        response = {
            "schema": "literate-ai/specification-acceptance@1",
            "specification_set_id": specification.specification_set_id,
            "source_snapshot_id": specification.source_snapshot_id,
            "authority_scope": "intent",
            "release_implementation_authority": "source-baseline",
            "qualification_required": True,
            "target": args.target,
        }
        if promoted_project is not None:
            response["promoted_project"] = promoted_project
        return response, 0
    if command == "qualify":
        if not args.allow_host_execution:
            raise CliFailure(
                "qualification.host_execution_not_authorized",
                "local build, test, and parity execution requires "
                "--allow-host-execution",
            )
        component = Path(args.component).resolve(strict=True)
        component_root = component.parent if component.is_file() else component
        project = discover_project(component_root)
        if project is None:
            raise CliFailure(
                "qualification.project_required",
                "qualification requires a canonical promoted project",
            )
        profile_path = Path(args.profile).resolve(strict=True)
        if (
            profile_path.is_symlink()
            or not profile_path.is_file()
            or not profile_path.is_relative_to(project.root)
        ):
            raise CliFailure(
                "qualification.profile_untrusted",
                "qualification profile must be a regular file inside the project",
            )
        profile = LocalQualificationProfile.from_dict(_read_json(profile_path))
        from literate_ai.adapters.authority import (
            AuthorityProjectionStoreError,
            FileAuthorityProjectionStore,
        )
        from literate_ai.adapters.locked_generation_authority import (
            FilesystemLockedGenerationAuthorityReader,
            LockedGenerationAuthorityReaderError,
        )
        from literate_ai.adapters.project_initialization import (
            flavor_selector_directory,
        )
        from literate_ai.application import SourcePromotionService
        from literate_ai.authority import ComponentAuthorityLifecycle
        from literate_ai.source_to_specification.promotion_materialization import (
            PromotionInputKind,
            SourcePromotionError,
        )

        try:
            locked_snapshot = FilesystemLockedGenerationAuthorityReader().read(
                component_root,
                target_name=args.target,
                flavor_selectors=tuple(args.flavor),
            )
        except LockedGenerationAuthorityReaderError as exc:
            raise CliFailure(exc.code, exc.message) from exc
        locked_authority = locked_snapshot.authority
        root_node = next(
            node
            for node in locked_authority.lock.nodes
            if node.revision.identity == locked_authority.lock.root_revision
        )
        component_coordinate = locked_authority.root_authoring.coordinate.uri
        component_revision_identity = root_node.revision.identity
        locked_snapshot.require_unchanged()
        authority_store = FileAuthorityProjectionStore(project.root)
        try:
            authority_projection = authority_store.current(component_coordinate)
        except AuthorityProjectionStoreError as exc:
            raise CliFailure(
                "qualification.authority_projection_required",
                "qualification requires a valid Component authority projection",
            ) from exc
        authority_status = ComponentAuthorityLifecycle.status(
            authority_projection,
            component_revision_identity=component_revision_identity,
        )
        stale_blockers = tuple(
            blocker
            for blocker in authority_status.blockers
            if blocker != "regenerative-qualification-required"
        )
        if (
            authority_status.recorded_state
            is not ComponentAuthorityState.DERIVED_SOURCE_RETAINED
            or stale_blockers
            or authority_projection.specification_set_identity
            != root_node.revision.specification_set_identity
        ):
            raise CliFailure(
                "qualification.authority_state_invalid",
                "qualification requires current retained-source authority after "
                "human acceptance",
            )
        promotion_service = SourcePromotionService()
        try:
            verified_promotion = promotion_service.verify_evidence(
                project.root, authority_projection
            )
            component_audit = verified_promotion.audit_containing(
                (component_root.relative_to(project.root) / "component.md").as_posix()
            )
        except (SourcePromotionError, ValueError) as exc:
            raise CliFailure(
                "qualification.promotion_evidence_invalid",
                "qualification requires the exact current promotion audit",
            ) from exc
        promotion_translation = verified_promotion.translation
        if (
            isinstance(promotion_translation, dict)
            and promotion_translation.get("mode") == "deterministic-static"
        ):
            if verified_promotion.inverse_evidence_custody is not None:
                raise CliFailure(
                    "qualification.inverse_evidence_invalid",
                    "static promotion must not retain model inverse evidence custody",
                )
        else:
            try:
                validate_qualification_inverse_evidence(
                    translation=promotion_translation,
                    custody=verified_promotion.inverse_evidence_custody,
                )
            except SourceToSpecificationError as exc:
                if exc.code == "inverse_evidence.required":
                    raise CliFailure(
                        "qualification.inverse_evidence_required",
                        "semantic qualification requires retained complete inverse "
                        "evidence custody",
                    ) from exc
                raise CliFailure(
                    "qualification.inverse_evidence_invalid",
                    "retained inverse evidence does not prove complete bounded model "
                    "admission",
                ) from exc
        source = Path(args.source).resolve(strict=True)
        if _contained(project.root, source) or _contained(source, project.root):
            raise CliFailure(
                "qualification.source_project_overlap",
                "source baseline and promoted project must be separate trees",
            )
        output_path = Path(args.output).resolve(strict=False)
        if _contained(source, output_path) or _contained(project.root, output_path):
            raise CliFailure(
                "qualification.output_overlap",
                "qualification output must be outside the source baseline and "
                "promoted project trees",
            )
        inventory = inventory_source(source)
        source_snapshot_id = authority_projection.source_snapshot_identity.uri
        specification_set_id = (
            None
            if authority_projection.specification_set_identity is None
            else authority_projection.specification_set_identity.uri
        )
        if inventory.identity != source_snapshot_id:
            raise CliFailure(
                "qualification.source_mismatch",
                "source baseline does not match the accepted source snapshot",
            )
        source_exclusion = promotion_service.assess_source_exclusion(
            component_audit, inventory
        )
        if not source_exclusion.source_excluded:
            raise CliFailure(
                "qualification.source_in_generation_inputs",
                "accepted source baseline overlaps forward generation inputs",
            )
        if not isinstance(specification_set_id, str):
            raise CliFailure(
                "qualification.accepted_set_invalid",
                "accepted specification set identity is missing",
            )
        from .generation import plan_from_args

        plan_report = plan_from_args(
            Namespace(
                specification=str(component_root),
                recipe_id=None,
                model=getattr(args, "model", None),
                target=args.target,
                flavor=list(args.flavor),
                flavor_root=[],
            )
        )
        planned_component = _object(
            plan_report.get("component"), "generation Component"
        )
        if (
            planned_component.get("revision_identity")
            != component_revision_identity.uri
        ):
            raise CliFailure(
                "qualification.authority_state_invalid",
                "qualification plan does not use the accepted Component revision",
            )
        resolution = _object(plan_report.get("resolution"), "generation resolution")
        component_lock_id = resolution.get("component_lock_identity")
        if component_lock_id != locked_authority.lock.identity.uri:
            raise CliFailure(
                "qualification.plan_invalid",
                "qualification plan does not use the current exact Component lock",
            )
        selected_flavors = plan_report.get("selected_flavors", [])
        if not isinstance(selected_flavors, list):
            raise CliFailure(
                "qualification.plan_invalid", "selected Flavors are invalid"
            )
        target_profile_id = resolution.get("target_profile_identity")
        if target_profile_id != locked_authority.lock.target_profile_identity.uri:
            raise CliFailure(
                "qualification.plan_invalid",
                "qualification plan does not use the locked target profile",
            )
        if not isinstance(plan_report.get("recipe_identity"), str):
            raise CliFailure(
                "qualification.plan_invalid", "generation recipe identity is missing"
            )
        try:
            selected_flavor_ids = tuple(
                str(_object(item, "selected Flavor")["id"]) for item in selected_flavors
            )
            flavor_set_identity = generation_input_subset_identity(
                component_audit,
                kinds=frozenset({PromotionInputKind.REVIEWED_FLAVOR}),
                target_prefixes=tuple(
                    f"flavors/{flavor_selector_directory(f'+{flavor_id}')}"
                    for flavor_id in selected_flavor_ids
                ),
            )
            skill_set_identity = generation_input_subset_identity(
                component_audit,
                kinds=frozenset({PromotionInputKind.FORWARD_SKILL}),
            )
            workflow_reference = _object(
                _object(plan_report.get("workflow"), "generation workflow").get(
                    "reference"
                ),
                "generation workflow reference",
            )
            routing_reference = _object(
                _object(plan_report.get("routing"), "generation routing").get(
                    "reference"
                ),
                "generation routing reference",
            )
            workflow_identity = ContentIdentity.from_dict(
                _object(
                    workflow_reference.get("identity"),
                    "generation workflow identity",
                )
            )
            routing_identity = ContentIdentity.from_dict(
                _object(
                    routing_reference.get("identity"),
                    "generation routing identity",
                )
            )
        except (KeyError, TypeError, ValueError, SourcePromotionError) as exc:
            raise CliFailure(
                "qualification.plan_invalid",
                "generation plan does not close its audited Flavor, skill, workflow, "
                "and routing inputs",
            ) from exc
        return _complete_standard_lifecycle_qualification(
            args=args,
            project=project,
            component_root=component_root,
            source=source,
            profile=profile,
            authority_store=authority_store,
            authority_projection=authority_projection,
            locked_snapshot=locked_snapshot,
            component_coordinate=component_coordinate,
            specification_set_identity=ContentIdentity.parse_uri(specification_set_id),
            source_snapshot_identity=ContentIdentity.parse_uri(source_snapshot_id),
            component_audit=component_audit,
            flavor_set_identity=flavor_set_identity,
            skill_set_identity=skill_set_identity,
            workflow_identity=workflow_identity,
            routing_identity=routing_identity,
            output_path=output_path,
        )

    if command == "refresh":
        _previous_bundle, previous = _unwrap_bundle(args.previous)
        descriptor, case = _load_case(args.case)
        bundle, result, digests = _derive(
            descriptor,
            case,
            mode=RunMode.REFRESH,
            previous_specification_set_id=(
                previous.request.previous_specification_set_id
                or previous.draft.draft_id
            ),
            skills_root=args.skills,
        )
        catalog = _load_discovered_skill_catalog(descriptor, args.skills)
        selected = builtin_skill_set(catalog)
        changes = tuple(args.changed_path) or _strings(
            case.get("changed_source_paths", []), "changed_source_paths"
        )
        invalidation = plan_refresh(
            previous,
            result.request,
            previous_skill_set=selected,
            new_skill_set=selected,
            current_evidence_digests=digests,
            changed_source_paths=changes,
        )
        return {
            "schema": "literate-ai/source-to-specification-refresh@1",
            "bundle": bundle,
            "invalidation": canonical_value(invalidation),
        }, 0
    if command == "conformance":
        descriptor, case = _load_case(args.case)
        report = _conformance(descriptor, case, skills_root=args.skills)
        return report, 0 if report["passed"] else 1
    if command == "coverage":
        _bundle, result = _unwrap_bundle(args.bundle)
        counts = Counter(item.state.value for item in result.coverage.entries)
        return {
            "schema": "literate-ai/source-specification-coverage-report@1",
            "request_id": result.request.request_id,
            "counts": {key: counts[key] for key in sorted(counts)},
            "entries": canonical_value(result.coverage.entries),
        }, 0
    if command == "diff":
        return {
            "schema": "literate-ai/specification-artifact-diff@1",
            **_diff(_artifact_map(args.left), _artifact_map(args.right)),
        }, 0
    raise CliFailure("cli.usage", "a spec command is required")


def add_spec_parser(commands: argparse._SubParsersAction) -> None:
    spec = commands.add_parser(
        "spec", help="derive, review, accept, refresh, and compare specifications"
    )
    spec_commands = spec.add_subparsers(
        dest="spec_command", required=True, parser_class=JsonArgumentParser
    )

    corpus_format = spec_commands.add_parser(
        "format", help="preview or write canonical literate Markdown frontmatter"
    )
    corpus_format.add_argument("corpus", help="corpus directory or root spec.md")
    corpus_format.add_argument("--id-prefix", required=True)
    format_mode = corpus_format.add_mutually_exclusive_group()
    format_mode.add_argument(
        "--check", action="store_true", help="fail when formatting would change files"
    )
    format_mode.add_argument(
        "--write", action="store_true", help="atomically replace changed documents"
    )

    corpus_validate = spec_commands.add_parser(
        "validate", help="validate a layered corpus without changing it"
    )
    corpus_validate.add_argument("corpus", help="corpus directory or root spec.md")
    corpus_validate.add_argument("--id-prefix", required=True)

    corpus_explain = spec_commands.add_parser(
        "explain", help="show derived IDs, parents, references, and effective context"
    )
    corpus_explain.add_argument("corpus", help="corpus directory or root spec.md")
    corpus_explain.add_argument("--id-prefix", required=True)
    corpus_explain.add_argument("--node", help="explain one derived node ID")

    scxml_review = spec_commands.add_parser(
        "scxml-review",
        help="validate one exact SCXML chart and its explicit trace sidecars",
    )
    scxml_review.add_argument("chart", help="SCXML 1.0 chart to review")
    scxml_review.add_argument(
        "--trace",
        action="append",
        default=[],
        help="explicit .trace.json sidecar; repeat for every trace document",
    )

    attest = spec_commands.add_parser("attest")
    attest.add_argument("source")
    attest.add_argument("--signer", required=True)
    attest.add_argument("--machine", required=True)
    attest.add_argument("--key", required=True)

    skills = spec_commands.add_parser("skills")
    skills.add_argument("--skills", "--skills-root", dest="skills")

    status = spec_commands.add_parser("status")
    status.add_argument("project", nargs="?", default=".")
    status.add_argument("--component")
    status.add_argument("--json", action="store_true")

    merge = spec_commands.add_parser(
        "merge",
        help=(
            "reverse-adopt one hand-authored island in an already-managed project "
            "without quarantining the tree"
        ),
    )
    merge.add_argument("source", help="project-relative source island to adopt")
    merge.add_argument(
        "--project",
        default=".",
        help="already-managed literate-ai project root (default: current directory)",
    )
    merge.add_argument(
        "--component",
        required=True,
        help="new Component directory name under components/",
    )
    merge.add_argument(
        "--apply",
        action="store_true",
        help="write the proposed component.md; planning is the default",
    )

    derive = spec_commands.add_parser("derive")
    derive.add_argument("case")
    derive.add_argument("--skills", "--skills-root", dest="skills")
    derive.add_argument("--attestation")
    derive.add_argument("--trust-key")
    derive.add_argument(
        "--translator",
        choices=("static", "coding-cli"),
        default="static",
        help="use inert inventory or the coding-CLI translator (unavailable)",
    )
    derive.add_argument(
        "--allow-model-egress",
        action="store_true",
        help="authorize bounded classified source evidence to reach the selected model",
    )
    derive.add_argument("--model", help="explicit coding-CLI model selector")
    derive.add_argument(
        "--model-evidence-byte-budget",
        type=int,
        default=262_144,
        help="maximum UTF-8 source-evidence bytes admitted to one coding-agent call",
    )

    audit = spec_commands.add_parser("audit")
    audit.add_argument("case")
    audit.add_argument("--baseline", required=True)
    audit.add_argument("--skills", "--skills-root", dest="skills")
    audit.add_argument("--attestation")
    audit.add_argument("--trust-key")
    audit.add_argument(
        "--translator", choices=("static", "coding-cli"), default="static"
    )
    audit.add_argument("--allow-model-egress", action="store_true")
    audit.add_argument("--model")
    audit.add_argument("--model-evidence-byte-budget", type=int, default=262_144)

    review = spec_commands.add_parser("review")
    review.add_argument("bundle")
    review.add_argument("--actor", default="local-human")
    review.add_argument("--reason", default="reviewed exact source-backed draft")
    review.add_argument("--resolve", action="append", default=[])
    review.add_argument("--key")
    review.add_argument(
        "--component-graph",
        help=(
            "review and sign an explicit source-bound Component graph for static "
            "project promotion"
        ),
    )

    accept = spec_commands.add_parser("accept")
    accept.add_argument("case")
    accept.add_argument("bundle")
    accept.add_argument("--review", required=True)
    accept.add_argument("--target", required=True)
    accept.add_argument("--trust-key")
    project_destination = accept.add_mutually_exclusive_group()
    project_destination.add_argument(
        "--project-target",
        help=(
            "atomically create a canonical spec-only project with a complete "
            "Component and proposed language Flavors"
        ),
    )
    project_destination.add_argument(
        "--integrate-project",
        help=(
            "atomically create and register a source-free native child project "
            "inside an existing retained adopted project"
        ),
    )
    accept.add_argument("--project-id")
    accept.add_argument(
        "--qualification-target",
        default="host",
        help="target name recorded in the promoted Component lock",
    )
    accept.add_argument(
        "--flavor",
        action="append",
        default=[],
        help=(
            "ordered +flavor/-flavor selector for the promoted qualification lock; "
            "repeatable"
        ),
    )
    accept.add_argument(
        "--qualification-profile",
        help=(
            "reviewed local execution profile to pin as the promoted Component's "
            "independent acceptance contract"
        ),
    )

    qualify = spec_commands.add_parser(
        "qualify",
        help="run measured clean generation, build, test, parity, and attestation",
    )
    qualify.add_argument("component")
    qualify.add_argument("--source", required=True)
    qualify.add_argument("--profile", required=True)
    qualify.add_argument("--output", required=True)
    qualify.add_argument("--key", help=argparse.SUPPRESS)
    qualify.add_argument("--signer", help=argparse.SUPPRESS)
    qualify.add_argument("--target", default="host")
    qualify.add_argument(
        "--model", help="explicit coding-model selector for each clean generation run"
    )
    qualify.add_argument("--flavor", action="append", default=[])
    qualify.add_argument("--scratch-root")
    qualify.add_argument(
        "--retained-evidence-store",
        help=(
            "explicit local immutable evidence store for captured library packages "
            "and qualification records; does not authorize importer admission"
        ),
    )
    qualify.add_argument("--allow-host-execution", action="store_true")

    refresh = spec_commands.add_parser("refresh")
    refresh.add_argument("case")
    refresh.add_argument("--previous", required=True)
    refresh.add_argument("--changed-path", action="append", default=[])
    refresh.add_argument("--skills", "--skills-root", dest="skills")

    conformance = spec_commands.add_parser("conformance")
    conformance.add_argument("case")
    conformance.add_argument("--skills", "--skills-root", dest="skills")

    coverage = spec_commands.add_parser("coverage")
    coverage.add_argument("bundle")

    difference = spec_commands.add_parser("diff")
    difference.add_argument("left")
    difference.add_argument("right")
