"""Shared test fixtures extracted from test_source_generation_boundary_contracts."""

from __future__ import annotations

from literate_ai.contracts import (
    GeneratedSourceCandidate,
    SourceGenerationProvenance,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def _candidate() -> GeneratedSourceCandidate:
    return GeneratedSourceCandidate(
        component_revision=_identity("component"),
        source_generation_request_identity=_identity("orchestration-request"),
        planned_coding_cli_request_identity=_identity("planned-coding-cli-request"),
        component_generation_plan_identity=_identity("plan"),
        generation_key_identity=_identity("key"),
        context_manifest_identity=_identity("context"),
        prompt_identity=_identity("prompt"),
        recipe_identity=_identity("recipe"),
        workspace_allocation_identity=_identity("workspace"),
        tree_identity=_identity("tree"),
        source_bundle_identity=_identity("bundle"),
        source_manifest_identity=_identity("manifest"),
        source_bom_identity=_identity("bom"),
        generated_test_suite_identity=_identity("generated-test-suite"),
    )


def _provenance(candidate: GeneratedSourceCandidate) -> SourceGenerationProvenance:
    return SourceGenerationProvenance(
        source_generation_request_identity=(
            candidate.source_generation_request_identity
        ),
        planned_coding_cli_request_identity=(
            candidate.planned_coding_cli_request_identity
        ),
        component_lock_identity=_identity("lock"),
        application_root_revision_identity=_identity("application-root"),
        generated_component_revision_identity=candidate.component_revision,
        component_generation_plan_identity=(
            candidate.component_generation_plan_identity
        ),
        generation_key_identity=candidate.generation_key_identity,
        context_manifest_identity=candidate.context_manifest_identity,
        prompt_identity=candidate.prompt_identity,
        recipe_identity=candidate.recipe_identity,
        workspace_allocation_identity=candidate.workspace_allocation_identity,
        readiness_identity=_identity("readiness"),
        route_decision_identities=(_identity("route-plan"), _identity("route-code")),
        model_stage_output_identities=(
            _identity("output-plan"),
            _identity("output-code"),
        ),
        candidate_identity=candidate.identity,
    )
