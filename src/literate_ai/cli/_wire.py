"""Strict wire codecs for the source-to-specification CLI adapter."""

from __future__ import annotations

from typing import Any

from literate_ai.contracts.library_imports import AuthoredLibraryImport
from literate_ai.source_to_specification import (
    V3_COMPONENT_GRAPH_DRAFT_SCHEMA,
    V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA,
    BehaviorObservation,
    ClaimKind,
    ComponentCapabilityContractDraft,
    ComponentDefinitionDraft,
    ComponentEntrypointDraft,
    ComponentGraphDraft,
    ComponentGraphEdgeDraft,
    ComponentGraphNodeDraft,
    CoverageEntry,
    CoverageState,
    DraftArtifact,
    DraftScenario,
    DraftStatement,
    EvidenceReference,
    ProviderValidation,
    ReviewAuthority,
    ReviewDisposition,
    ReviewStatementDecision,
    RunMode,
    SkillRef,
    SourceSpecificationCoverageMap,
    SourceToSpecificationRequest,
    SourceToSpecificationResult,
    SourceToSpecificationRun,
    SpecificationDraftSet,
    SpecificationReviewDecision,
    UncertaintyItem,
    UncertaintyKind,
    UncertaintyLedger,
    canonical_value,
    normalize_source_to_specification_result,
)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _items(value: Any, name: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return tuple(value)


def _strings(value: Any, name: str) -> tuple[str, ...]:
    items = _items(value, name)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"{name} must contain strings")
    return items


def _skill_ref(value: Any) -> SkillRef:
    data = _mapping(value, "skill")
    return SkillRef(data["skill_id"], data["version"], data["content_digest"])


def _evidence(value: Any) -> EvidenceReference:
    data = _mapping(value, "evidence")
    return EvidenceReference(
        evidence_id=data["evidence_id"],
        source_snapshot_id=data["source_snapshot_id"],
        content_digest=data["content_digest"],
        path=data["path"],
        symbol=data.get("symbol", ""),
    )


def _statement(value: Any) -> DraftStatement:
    data = _mapping(value, "statement")
    return DraftStatement(
        statement_id=data["statement_id"],
        capability=data["capability"],
        requirement=data["requirement"],
        scenarios=tuple(
            DraftScenario(**_mapping(item, "scenario"))
            for item in _items(data["scenarios"], "scenarios")
        ),
        observation_ids=_strings(data["observation_ids"], "observation_ids"),
    )


def component_graph_from_wire(value: Any) -> ComponentGraphDraft:
    graph_data = _mapping(value, "component graph draft")
    return ComponentGraphDraft(
        source_snapshot_id=graph_data["source_snapshot_id"],
        root_coordinate=graph_data["root_coordinate"],
        nodes=tuple(
            ComponentGraphNodeDraft(
                coordinate=node["coordinate"],
                title=node["title"],
                kind=node.get("kind", "unknown"),
                profiles=_strings(node.get("profiles", []), "profiles"),
                provided_capabilities=_strings(
                    node["provided_capabilities"], "provided_capabilities"
                ),
                capability_contracts=tuple(
                    ComponentCapabilityContractDraft(
                        name=contract["name"],
                        contract=contract["contract"],
                        evidence_ids=_strings(contract["evidence_ids"], "evidence_ids"),
                    )
                    for raw_contract in _items(
                        node.get("capability_contracts", []),
                        "capability contracts",
                    )
                    for contract in (_mapping(raw_contract, "capability contract"),)
                ),
                entrypoints=tuple(
                    ComponentEntrypointDraft(
                        name=entrypoint["name"],
                        kind=entrypoint["kind"],
                        path=entrypoint["path"],
                        evidence_ids=_strings(
                            entrypoint["evidence_ids"], "evidence_ids"
                        ),
                    )
                    for raw_entrypoint in _items(
                        node.get("entrypoints", []), "entrypoints"
                    )
                    for entrypoint in (_mapping(raw_entrypoint, "entrypoint"),)
                ),
                library_imports=tuple(
                    AuthoredLibraryImport.from_dict(item)
                    for item in _items(
                        node.get("library_imports", []), "library imports"
                    )
                ),
                build_needs=_strings(node.get("build_needs", []), "build needs"),
                source_paths=_strings(node["source_paths"], "source_paths"),
                observation_ids=_strings(node["observation_ids"], "observation_ids"),
                evidence_ids=_strings(node["evidence_ids"], "evidence_ids"),
            )
            for raw in _items(graph_data["nodes"], "component graph nodes")
            for node in (_mapping(raw, "component graph node"),)
        ),
        edges=tuple(
            ComponentGraphEdgeDraft(
                source_coordinate=edge["source_coordinate"],
                target_coordinate=edge["target_coordinate"],
                requirement_id=edge["requirement_id"],
                capability=edge["capability"],
                version_range=edge["version_range"],
                dependency_kind=edge["dependency_kind"],
                optional=edge["optional"],
                evidence_ids=_strings(edge["evidence_ids"], "evidence_ids"),
            )
            for raw in _items(graph_data["edges"], "component graph edges")
            for edge in (_mapping(raw, "component graph edge"),)
        ),
    )


def _artifact(value: Any) -> DraftArtifact:
    return DraftArtifact(**_mapping(value, "artifact"))


def _validation(value: Any) -> ProviderValidation:
    data = _mapping(value, "validation")
    return ProviderValidation(
        provider=data["provider"],
        provider_version=data["provider_version"],
        valid=data["valid"],
        diagnostics=_strings(data.get("diagnostics", []), "diagnostics"),
    )


def result_to_wire(result: SourceToSpecificationResult) -> dict[str, Any]:
    document = canonical_value(result)
    graph = document.get("component_graph_draft")
    if graph is not None:
        legacy = any(
            node.kind == "unknown" for node in result.component_graph_draft.nodes
        )
        if legacy:
            for node in graph["nodes"]:
                for field in (
                    "kind",
                    "profiles",
                    "capability_contracts",
                    "entrypoints",
                    "build_needs",
                ):
                    node.pop(field)
            document["component_graph_draft"] = {
                "schema": "urn:literate-ai:schema:v2:component-graph-draft",
                **graph,
            }
            return {
                "schema": "urn:literate-ai:schema:v2:source-to-specification-result",
                **document,
            }
        document["component_graph_draft"] = {
            "schema": V3_COMPONENT_GRAPH_DRAFT_SCHEMA,
            **graph,
        }
    return {"schema": V3_SOURCE_TO_SPECIFICATION_RESULT_SCHEMA, **document}


def result_from_wire(value: Any) -> SourceToSpecificationResult:
    data = normalize_source_to_specification_result(value)
    request_data = _mapping(data["request"], "request")
    source_request = SourceToSpecificationRequest(
        request_id=request_data["request_id"],
        source_snapshot_id=request_data["source_snapshot_id"],
        source_content_digest=request_data["source_content_digest"],
        origin_attestation_id=request_data["origin_attestation_id"],
        mode=RunMode(request_data["mode"]),
        output_provider=request_data["output_provider"],
        skill_set_id=request_data["skill_set_id"],
        routing_policy_id=request_data["routing_policy_id"],
        redaction_policy_id=request_data["redaction_policy_id"],
        egress_policy_id=request_data["egress_policy_id"],
        included_paths=_strings(request_data["included_paths"], "included_paths"),
        excluded_paths=_strings(request_data["excluded_paths"], "excluded_paths"),
        facets=_strings(request_data["facets"], "facets"),
        previous_specification_set_id=request_data["previous_specification_set_id"],
    )
    observations = tuple(
        BehaviorObservation(
            observation_id=item["observation_id"],
            surface_ids=_strings(item["surface_ids"], "surface_ids"),
            facet=item["facet"],
            claim_kind=ClaimKind(item["claim_kind"]),
            statement=item["statement"],
            evidence=tuple(
                _evidence(reference)
                for reference in _items(item["evidence"], "evidence")
            ),
            skill=_skill_ref(item["skill"]),
            confidence=item["confidence"],
            model_call_id=item["model_call_id"],
        )
        for raw in _items(data["observations"], "observations")
        for item in (_mapping(raw, "observation"),)
    )
    draft_data = _mapping(data["draft"], "draft")
    draft = SpecificationDraftSet(
        draft_id=draft_data["draft_id"],
        request_id=draft_data["request_id"],
        source_snapshot_id=draft_data["source_snapshot_id"],
        output_provider=draft_data["output_provider"],
        skill_set_identity=draft_data["skill_set_identity"],
        statements=tuple(
            _statement(item) for item in _items(draft_data["statements"], "statements")
        ),
        artifacts=tuple(
            _artifact(item) for item in _items(draft_data["artifacts"], "artifacts")
        ),
        validation=_validation(draft_data["validation"]),
    )
    coverage_data = _mapping(data["coverage"], "coverage")
    coverage = SourceSpecificationCoverageMap(
        request_id=coverage_data["request_id"],
        entries=tuple(
            CoverageEntry(
                surface_id=item["surface_id"],
                state=CoverageState(item["state"]),
                observation_ids=_strings(item["observation_ids"], "observation_ids"),
                statement_ids=_strings(item["statement_ids"], "statement_ids"),
                reason=item["reason"],
            )
            for raw in _items(coverage_data["entries"], "coverage entries")
            for item in (_mapping(raw, "coverage entry"),)
        ),
    )
    uncertainty_data = _mapping(data["uncertainty"], "uncertainty")
    uncertainty = UncertaintyLedger(
        request_id=uncertainty_data["request_id"],
        items=tuple(
            UncertaintyItem(
                uncertainty_id=item["uncertainty_id"],
                kind=UncertaintyKind(item["kind"]),
                message=item["message"],
                observation_ids=_strings(item["observation_ids"], "observation_ids"),
                surface_ids=_strings(item["surface_ids"], "surface_ids"),
                blocking=item["blocking"],
            )
            for raw in _items(uncertainty_data["items"], "uncertainty items")
            for item in (_mapping(raw, "uncertainty item"),)
        ),
    )
    run_data = _mapping(data["run"], "run")
    run = SourceToSpecificationRun(
        run_id=run_data["run_id"],
        request_identity=run_data["request_identity"],
        skill_set_identity=run_data["skill_set_identity"],
        observation_ids=_strings(run_data["observation_ids"], "observation_ids"),
        draft_identity=run_data["draft_identity"],
    )
    component_value = data.get("component_definition_draft")
    component_definition_draft = None
    if component_value is not None:
        component_data = _mapping(component_value, "component definition draft")
        component_definition_draft = ComponentDefinitionDraft(
            coordinate=component_data["coordinate"],
            title=component_data["title"],
            provided_capabilities=_strings(
                component_data["provided_capabilities"], "provided_capabilities"
            ),
            source_snapshot_id=component_data["source_snapshot_id"],
        )
    graph_value = data.get("component_graph_draft")
    component_graph_draft = (
        None if graph_value is None else component_graph_from_wire(graph_value)
    )
    return SourceToSpecificationResult(
        request=source_request,
        observations=observations,
        draft=draft,
        coverage=coverage,
        uncertainty=uncertainty,
        run=run,
        component_definition_draft=component_definition_draft,
        component_graph_draft=component_graph_draft,
    )


def review_to_wire(review: SpecificationReviewDecision) -> dict[str, Any]:
    return canonical_value(review)


def review_from_wire(value: Any) -> SpecificationReviewDecision:
    data = _mapping(value, "review")
    return SpecificationReviewDecision(
        review_id=data["review_id"],
        draft_id=data["draft_id"],
        base_specification_set_id=data["base_specification_set_id"],
        actor=data["actor"],
        authority=ReviewAuthority(data["authority"]),
        reason=data["reason"],
        statement_decisions=tuple(
            ReviewStatementDecision(
                statement_id=item["statement_id"],
                disposition=ReviewDisposition(item["disposition"]),
                reason=item["reason"],
                replacement=(
                    None
                    if item["replacement"] is None
                    else _statement(item["replacement"])
                ),
            )
            for raw in _items(data["statement_decisions"], "statement decisions")
            for item in (_mapping(raw, "statement decision"),)
        ),
        resolved_uncertainty_ids=_strings(
            data["resolved_uncertainty_ids"], "resolved_uncertainty_ids"
        ),
        resulting_artifacts=tuple(
            _artifact(item)
            for item in _items(data["resulting_artifacts"], "resulting artifacts")
        ),
        validation=_validation(data["validation"]),
        component_graph_draft=(
            None
            if data.get("component_graph_draft") is None
            else component_graph_from_wire(data["component_graph_draft"])
        ),
    )


__all__ = [
    "component_graph_from_wire",
    "result_from_wire",
    "result_to_wire",
    "review_from_wire",
    "review_to_wire",
]
