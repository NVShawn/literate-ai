from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_standard_source_admission``."""


from literate_ai.contracts import (
    GeneratedSourceCandidate,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    canonical_identity,
)


def identity(label: str):
    return canonical_identity({"standard-source-admission-test": label})


def generation(
    *,
    node: str = "root",
    orchestration: str = "orchestration-request",
) -> SourceGenerationResumeCandidate:
    candidate = GeneratedSourceCandidate(
        identity(f"component-{node}"),
        identity(orchestration),
        identity(f"planned-coding-cli-request-{node}"),
        identity(f"plan-{node}"),
        identity(f"key-{node}"),
        identity(f"context-{node}"),
        identity(f"prompt-{node}"),
        identity(f"recipe-{node}"),
        identity(f"workspace-{node}"),
        identity(f"tree-{node}"),
        identity(f"bundle-{node}"),
        identity(f"manifest-{node}"),
        identity(f"source-bom-{node}"),
        identity(f"suite-{node}"),
    )
    provenance = SourceGenerationProvenance(
        candidate.source_generation_request_identity,
        candidate.planned_coding_cli_request_identity,
        identity("lock"),
        identity("application"),
        candidate.component_revision,
        candidate.component_generation_plan_identity,
        candidate.generation_key_identity,
        candidate.context_manifest_identity,
        candidate.prompt_identity,
        candidate.recipe_identity,
        candidate.workspace_allocation_identity,
        identity("readiness"),
        (identity("route"),),
        (identity("coding-cli-transcript"),),
        candidate.identity,
    )
    output = SourceGenerationRunOutput(
        candidate,
        candidate.identity,
        provenance,
        provenance.identity,
    )
    return SourceGenerationResumeCandidate(
        output,
        output.identity,
        identity("budget"),
        identity("complexity"),
    )
