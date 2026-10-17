"""Source-only scheduling over complete prepared Component nodes."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.application import (
    PreparedComponentGenerationNode,
    prepare_component_generation_nodes,
)
from literate_ai.contracts import (
    ComponentGenerationRuntimeObservation,
    ContentIdentity,
    GeneratedSourceCandidate,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    canonical_identity,
)
from tests.support.fixtures_test_component_node_generation_preparation import (
    _budget,
    _fixture,
)


def _recipe_identity(node: PreparedComponentGenerationNode) -> ContentIdentity:
    return ContentIdentity.parse_uri(node.recipe.identity)


def _output(
    node: PreparedComponentGenerationNode,
    observation: ComponentGenerationRuntimeObservation | None = None,
    *,
    candidate_changes: dict[str, ContentIdentity] | None = None,
) -> SourceGenerationRunOutput:
    request = node.request.request
    recipe_identity = _recipe_identity(node)
    candidate = GeneratedSourceCandidate(
        component_revision=node.plan.component_revision,
        source_generation_request_identity=request.identity,
        planned_coding_cli_request_identity=canonical_identity(
            {"planned-request": node.plan.component_revision.uri}
        ),
        component_generation_plan_identity=node.plan.identity,
        generation_key_identity=node.plan.generation_key.identity,
        context_manifest_identity=request.context_manifest_identity,
        prompt_identity=request.prompt_identity,
        recipe_identity=recipe_identity,
        workspace_allocation_identity=node.workspace.allocation_identity,
        tree_identity=canonical_identity({"tree": node.plan.component_revision.uri}),
        source_bundle_identity=canonical_identity(
            {"bundle": node.plan.component_revision.uri}
        ),
        source_manifest_identity=canonical_identity(
            {"manifest": node.plan.component_revision.uri}
        ),
        source_bom_identity=canonical_identity(
            {"bom": node.plan.component_revision.uri}
        ),
        generated_test_suite_identity=canonical_identity(
            {"tests": node.plan.component_revision.uri}
        ),
    )
    if candidate_changes:
        candidate = replace(candidate, **candidate_changes)
    provenance = SourceGenerationProvenance(
        source_generation_request_identity=candidate.source_generation_request_identity,
        planned_coding_cli_request_identity=candidate.planned_coding_cli_request_identity,
        component_lock_identity=node.recipe.component_lock_identity,
        application_root_revision_identity=canonical_identity({"root": "fixture"}),
        generated_component_revision_identity=candidate.component_revision,
        component_generation_plan_identity=candidate.component_generation_plan_identity,
        generation_key_identity=candidate.generation_key_identity,
        context_manifest_identity=candidate.context_manifest_identity,
        prompt_identity=candidate.prompt_identity,
        recipe_identity=candidate.recipe_identity,
        workspace_allocation_identity=candidate.workspace_allocation_identity,
        readiness_identity=canonical_identity({"readiness": "fixture"}),
        route_decision_identities=(canonical_identity({"route": "fixture"}),),
        model_stage_output_identities=(canonical_identity({"stage": "fixture"}),),
        candidate_identity=candidate.identity,
    )
    return SourceGenerationRunOutput(
        candidate, candidate.identity, provenance, provenance.identity, observation
    )


def _resume(node: PreparedComponentGenerationNode) -> SourceGenerationResumeCandidate:
    output = _output(node)
    request = node.request.request
    return SourceGenerationResumeCandidate(
        output,
        output.identity,
        request.budget.identity,
        request.complexity_decision_identity,
    )


class _PreparedFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        snapshot, self.execution = _fixture()
        adapter = LockedComponentNodePreparationAdapter()
        allocator = FilesystemComponentWorkspaceAllocator(Path(self.temporary.name))
        self.nodes = prepare_component_generation_nodes(
            self.execution,
            authority=snapshot,
            authority_lock_identity=adapter.authority_lock_identity,
            authority_guard=adapter.guard,
            node_projector=adapter.project,
            workspace_allocator=allocator.allocate,
            framework_envelope=lambda projection: projection.recipe.prompt().encode(
                "utf-8"
            ),
            budget=_budget(),
        )
        self.by_uri = {node.plan.component_revision.uri: node for node in self.nodes}
        self.names = {
            node.plan.component_revision.uri: node.definition.coordinate.name
            for node in self.nodes
        }

    def close(self) -> None:
        self.temporary.cleanup()


class SourceGenerationSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _PreparedFixture()

    def tearDown(self) -> None:
        self.fixture.close()


if __name__ == "__main__":
    unittest.main()
