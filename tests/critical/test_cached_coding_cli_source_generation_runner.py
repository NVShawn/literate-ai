"""Production source-only adapter tests with a deterministic cached delegate seam."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import literate_ai.adapters.models.coding_cli as coding_cli_adapter
from literate_ai.adapters.cache import CachedCodingCliSourceGenerator
from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.models import CodingCliGeneration
from literate_ai.adapters.source_generation import (
    CachedCodingCliSourceGenerationError,
    CachedCodingCliSourceGenerationRunner,
    CodingCliSourceGenerationInvocation,
)
from literate_ai.application import (
    GenerationExecutionPlan,
    prepare_component_generation_nodes,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    CycloneDxLifecycle,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    canonical_identity,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GENERATED_TEST_SUITE_SCHEMA,
)
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_application_generation import router, stages
from tests.support.fixtures_test_component_node_generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
    LockedComponentNodePreparationAdapter,
    _budget,
    _fixture,
)


def _test_manifest(recipe) -> str:
    reference = recipe.non_acceptance_document_paths[0]
    return json.dumps(
        {
            "schema": GENERATED_TEST_SUITE_SCHEMA,
            "recipe_identity": recipe.identity,
            "generation_mode": "major-rebuild",
            "cases": [
                {
                    "case_id": "component-example",
                    "category": "example",
                    "specification_refs": [reference],
                    "arguments": [{"component": "example"}],
                    "expected_result": {"component": "example"},
                },
                {
                    "case_id": "component-boundary",
                    "category": "boundary",
                    "specification_refs": [reference],
                    "arguments": [{"component": "boundary"}],
                    "expected_result": {"component": "boundary"},
                },
                {
                    "case_id": "component-invariant",
                    "category": "invariant",
                    "specification_refs": [reference],
                    "arguments": [{"component": "invariant"}],
                    "expected_result": {"component": "invariant"},
                },
            ],
        },
        sort_keys=True,
    )


class _FakeCachedGenerator(CachedCodingCliSourceGenerator):
    """Cached-generator seam: one delegate result, then deterministic replay."""

    def __init__(self) -> None:
        self.delegate = SimpleNamespace(
            source_intelligence_provider=None,
            source_intelligence_mode="off",
        )
        self.calls = []
        self.delegate_calls = 0
        self.accept_invocations = 0
        self.publications = 0
        self.provider_evidence_identity: str | None = None
        self._cached: CodingCliGeneration | None = None
        self._pending: CodingCliGeneration | None = None

    def planned_request_identity(
        self, recipe, *, execution_plan, stage_request, bounded_prompt=None
    ) -> ContentIdentity:
        return canonical_identity(
            {
                "recipe": recipe.identity,
                "execution_plan": execution_plan.identity.uri,
                "stage_request": stage_request,
                "bounded_prompt_identity": (
                    None
                    if bounded_prompt is None
                    else "sha256:" + hashlib.sha256(bounded_prompt).hexdigest()
                ),
            }
        )

    def derivation_cache_key(
        self, recipe, *, execution_plan, stage_request, bounded_prompt=None
    ) -> SourceDerivationCacheKey:
        return SourceDerivationCacheKey(
            recipe_identity=ContentIdentity.parse_uri(recipe.identity),
            execution_plan_identity=execution_plan.identity,
            coding_cli_tool_binding_identity=ContentIdentity.parse_uri(
                "sha256:" + "b" * 64
            ),
            model_binding=SourceCacheModelBinding("codex", "fixture-model"),
            request_identity=self.planned_request_identity(
                recipe,
                execution_plan=execution_plan,
                stage_request=stage_request,
                bounded_prompt=bounded_prompt,
            ),
        )

    def generate(
        self,
        recipe,
        *,
        output_root,
        execution_plan,
        stage_request,
        bounded_prompt=None,
    ) -> CodingCliGeneration:
        self.calls.append(
            (recipe, Path(output_root), execution_plan, stage_request, bounded_prompt)
        )
        root = Path(output_root)
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise AssertionError("fake received a non-empty generation workspace")
        if self._cached is None:
            self.delegate_calls += 1
            assert recipe.managed_sbom_graph is not None
            authority_components, authority_edges = (
                coding_cli_adapter._recipe_authority_sbom(recipe)
            )
            sbom_content, sbom = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=recipe.managed_sbom_graph,
                additional_components=authority_components,
                additional_edges=authority_edges,
            )
            test_manifest = _test_manifest(recipe)
            files = {
                "source/main.py": "print('source-only')\n",
                GENERATED_TEST_SUITE_PATH: test_manifest,
                CYCLONEDX_SOURCE_SBOM_PATH: sbom_content.decode("utf-8"),
            }
            final_stage = execution_plan.model_stages[-1]
            final_route = execution_plan.route_decisions[-1]
            self._cached = CodingCliGeneration(
                files=files,
                coding_cli="codex",
                executable="/tools/codex",
                model="fixture-model",
                recipe_identity=recipe.identity,
                command=("/tools/codex", "generate"),
                request_identity=self.planned_request_identity(
                    recipe,
                    execution_plan=execution_plan,
                    stage_request=stage_request,
                    bounded_prompt=bounded_prompt,
                ).uri,
                execution_plan_identity=execution_plan.identity.uri,
                requested_model_stages=(final_stage.stage_id,),
                requested_route_decision_digests=(final_route.digest,),
                executable_identity="sha256:" + "e" * 64,
                command_identity="sha256:" + "d" * 64,
                coding_cli_selection_identity="sha256:" + "c" * 64,
                coding_cli_tool_binding_identity="sha256:" + "b" * 64,
                isolation_profile="fixture",
                hermetic=False,
                environment_keys=(),
                generated_test_suite_identity=(
                    "sha256:"
                    + hashlib.sha256(test_manifest.encode("utf-8")).hexdigest()
                ),
                source_sbom=sbom,
                source_intelligence_status="off",
                provider_evidence_identity=self.provider_evidence_identity,
            )
            self._pending = self._cached
        for path, content in self._cached.files.items():
            target = root.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        return self._cached

    def accept(self, generation) -> None:
        self.accept_invocations += 1
        if generation is self._pending:
            self.publications += 1
            self._pending = None


class CachedCodingCliSourceGenerationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve(strict=True)
        self.root = root
        snapshot, execution = _fixture()
        self.snapshot = snapshot
        adapter = LockedComponentNodePreparationAdapter()
        workspace_root = root / "workspaces"
        workspace_root.mkdir()
        allocator = FilesystemComponentWorkspaceAllocator(workspace_root)
        self.node = prepare_component_generation_nodes(
            execution,
            authority=snapshot,
            authority_lock_identity=adapter.authority_lock_identity,
            authority_guard=adapter.guard,
            node_projector=adapter.project,
            workspace_allocator=allocator.allocate,
            framework_envelope=lambda projection: projection.recipe.prompt().encode(
                "utf-8"
            ),
            budget=_budget(),
        )[0]
        stage_values = stages()
        route_selector = router()
        self.execution_plan = GenerationExecutionPlan(
            self.node.definition.workflow_definition,
            self.node.definition.routing_policy,
            stage_values,
            tuple(route_selector.select(stage.policy) for stage in stage_values),
        )
        self.stage_request = {
            "stage_id": self.execution_plan.model_stages[-1].stage_id,
            "prior_stage_outputs": {"plan": {"approved": True}},
            "input_identity": canonical_identity({"stage": "generate"}).to_dict(),
        }
        self.application_root = snapshot.authority.lock.root_revision
        self.readiness = canonical_identity({"readiness": "fixture"})
        self.invocation = CodingCliSourceGenerationInvocation.create(
            self.execution_plan,
            self.stage_request,
            application_root_revision_identity=self.application_root,
            readiness_identity=self.readiness,
        )
        self.generator = _FakeCachedGenerator()
        self.cas = FileSystemCAS(root / "cas")
        self.runner = CachedCodingCliSourceGenerationRunner(
            self.generator,
            cas=self.cas,
            invocation_provider=lambda _node: self.invocation,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _retained_input(self):
        from literate_ai.adapters.retained_source import RetainedSourceInput

        original = self.generator.generate(
            self.node.recipe,
            output_root=self.root / "retained",
            execution_plan=self.execution_plan,
            stage_request=self.stage_request,
        )
        retained = RetainedSourceInput.capture(
            self.root / "retained/source",
            component_lock_identity=self.node.recipe.component_lock_identity,
            project_authority_identity=canonical_identity({"project": "fixture"}),
            target="host",
        )
        self.runner.retained_source = retained
        self.generator.generate = mock.Mock(
            side_effect=AssertionError("must not generate")
        )
        return retained, original

    def test_retained_source_drift_prevents_candidate_and_publication(self):
        from literate_ai.adapters.retained_source import RetainedSourceError

        retained, _ = self._retained_input()
        output = self.runner(self.node)
        (retained.root / "main.py").write_text("changed\n")
        with self.assertRaises(RetainedSourceError):
            self.runner.cache_key_for_candidate(output.candidate)

    def test_retained_input_cannot_claim_model_provenance(self):
        self._retained_input()
        output = self.runner(self.node)
        with self.assertRaises(ValueError):
            replace(
                output.provenance,
                model_stage_output_identities=(canonical_identity({"fake": True}),),
            )

    def _fresh_node(self):
        root = self.root / f"fresh-{len(self.generator.calls)}"
        root.mkdir()
        return replace(
            self.node,
            workspace=replace(
                self.node.workspace,
                locator=str(root),
                allocation_identity=canonical_identity(
                    {
                        "schema": "literate-ai/component-workspace-allocation@1",
                        "component_revision": self.node.plan.component_revision.uri,
                        "generation_plan_identity": self.node.plan.identity.uri,
                        "locator": str(root.resolve(strict=True)),
                    }
                ),
            ),
        )

    def test_cache_publication_requires_the_exact_accepted_candidate(self) -> None:
        output = self.runner(self.node)
        self.assertEqual(self.generator.publications, 0)
        substituted = replace(
            output.candidate,
            tree_identity=canonical_identity({"tree": "substituted"}),
            source_bundle_identity=canonical_identity({"tree": "substituted"}),
        )
        with self.assertRaises(CachedCodingCliSourceGenerationError) as raised:
            self.runner.publish_accepted_candidate(substituted)
        self.assertEqual(
            raised.exception.code,
            "source_generation.candidate_not_pending",
        )
        self.assertEqual(self.generator.publications, 0)

        self.runner.publish_accepted_candidate(output.candidate)
        self.assertEqual(self.generator.accept_invocations, 1)
        self.assertEqual(self.generator.publications, 1)
        with self.assertRaises(CachedCodingCliSourceGenerationError):
            self.runner.publish_accepted_candidate(output.candidate)

        replay = self.runner(self._fresh_node())
        self.runner.publish_accepted_candidate(replay.candidate)
        self.assertEqual(self.generator.accept_invocations, 2)
        self.assertEqual(self.generator.publications, 1)


if __name__ == "__main__":
    unittest.main()
