"""Source-only scheduling over complete prepared Component nodes."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.application import (
    ComponentSourceGenerationRunError,
    PreparedComponentGenerationNode,
    SourceGenerationSchedulingError,
    execute_component_source_generation_node,
    prepare_component_generation_nodes,
    schedule_source_generation,
)
from literate_ai.contracts import (
    ComponentGenerationResumeCandidate,
    ComponentGenerationRuntimeObservation,
    ContentIdentity,
    GeneratedSourceCandidate,
    SourceGenerationDisposition,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    SourceGenerationScheduleResult,
    canonical_identity,
)
from tests.support.fixtures_test_component_generation_scheduling import _decision
from tests.support.fixtures_test_component_node_generation_preparation import (
    _budget,
    _fixture,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


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
        planned_coding_cli_request_identity=(
            candidate.planned_coding_cli_request_identity
        ),
        component_lock_identity=node.recipe.component_lock_identity,
        application_root_revision_identity=canonical_identity({"root": "fixture"}),
        generated_component_revision_identity=candidate.component_revision,
        component_generation_plan_identity=(
            candidate.component_generation_plan_identity
        ),
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
        candidate,
        candidate.identity,
        provenance,
        provenance.identity,
        observation,
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

    def test_exact_candidate_is_reused_without_invoking_runner(self) -> None:
        node = self.fixture.nodes[0]
        candidate = _resume(node)
        calls = []

        def runner(prepared):
            calls.append(prepared)
            return _output(prepared)

        execution = execute_component_source_generation_node(
            node,
            candidate=candidate,
            explicitly_invalid=False,
            runner=runner,
        )
        result = execution.result
        self.assertEqual(calls, [])
        self.assertEqual(execution.output, candidate.output)
        self.assertEqual(result.disposition, SourceGenerationDisposition.REUSED)
        self.assertEqual(result.candidate_identity, candidate.output.candidate_identity)
        self.assertIsNone(result.runtime_observation)

    def test_reuse_revalidates_every_generation_and_runtime_policy_identity(
        self,
    ) -> None:
        node = self.fixture.nodes[0]
        stale = canonical_identity({"stale": "identity"})
        candidate_fields = (
            "source_generation_request_identity",
            "component_generation_plan_identity",
            "generation_key_identity",
            "context_manifest_identity",
            "prompt_identity",
            "recipe_identity",
            "workspace_allocation_identity",
        )
        candidates = [
            SourceGenerationResumeCandidate(
                output,
                output.identity,
                node.request.request.budget.identity,
                node.request.request.complexity_decision_identity,
            )
            for output in (
                _output(node, candidate_changes={field: stale})
                for field in candidate_fields
            )
        ]
        exact = _resume(node)
        over_budget_output = _output(
            node,
            ComponentGenerationRuntimeObservation(
                node.request.request.budget.max_model_attempts + 1,
                None,
                None,
                None,
            ),
        )
        candidates.extend(
            (
                replace(exact, complexity_budget_identity=stale),
                replace(exact, complexity_decision_identity=stale),
                SourceGenerationResumeCandidate(
                    over_budget_output,
                    over_budget_output.identity,
                    exact.complexity_budget_identity,
                    exact.complexity_decision_identity,
                ),
            )
        )
        for candidate in candidates:
            calls = []

            def runner(prepared, calls=calls):
                calls.append(prepared)
                return _output(prepared)

            with self.subTest(candidate=candidate.identity.uri):
                execution = execute_component_source_generation_node(
                    node,
                    candidate=candidate,
                    explicitly_invalid=False,
                    runner=runner,
                )
                result = execution.result
                self.assertEqual(calls, [node])
                self.assertIsNotNone(execution.output)
                self.assertEqual(
                    result.disposition, SourceGenerationDisposition.GENERATED
                )

    def test_runtime_budget_failure_preserves_exact_measured_consumption(self) -> None:
        node = self.fixture.nodes[0]
        observation = ComponentGenerationRuntimeObservation(4, 17, None, 43)

        def runner(prepared):
            return _output(prepared, observation)

        execution = execute_component_source_generation_node(
            node,
            candidate=None,
            explicitly_invalid=False,
            runner=runner,
        )
        result = execution.result
        self.assertIsNone(execution.output)
        self.assertEqual(result.disposition, SourceGenerationDisposition.FAILED)
        self.assertEqual(result.failure_code, "runtime-budget-exceeded")
        self.assertEqual(result.runtime_observation, observation)
        self.assertIsNone(result.runtime_observation.model_tokens)

    def test_complete_prepared_node_is_required_and_revalidated(self) -> None:
        node = self.fixture.nodes[0]
        with self.assertRaises(TypeError):
            execute_component_source_generation_node(
                node.request,
                candidate=None,
                explicitly_invalid=False,
                runner=lambda prepared: _output(prepared),
            )
        wrong_workspace = replace(
            node.workspace,
            allocation_identity=canonical_identity({"allocation": "different"}),
            generation_plan_identity=canonical_identity({"plan": "different"}),
        )
        with self.assertRaises(SourceGenerationSchedulingError) as raised:
            execute_component_source_generation_node(
                replace(node, workspace=wrong_workspace),
                candidate=None,
                explicitly_invalid=False,
                runner=lambda prepared: _output(prepared),
            )
        self.assertEqual(
            raised.exception.code, "source_schedule.preparation_identity_mismatch"
        )
        with self.assertRaises(SourceGenerationSchedulingError) as raised:
            execute_component_source_generation_node(
                replace(node, request=replace(node.request, prompt=b"tampered")),
                candidate=None,
                explicitly_invalid=False,
                runner=lambda prepared: _output(prepared),
            )
        self.assertEqual(
            raised.exception.code, "source_schedule.prompt_identity_mismatch"
        )

    def test_runner_must_return_output_for_the_exact_complete_node(self) -> None:
        node = self.fixture.nodes[0]
        other = self.fixture.nodes[1]
        execution = execute_component_source_generation_node(
            node,
            candidate=None,
            explicitly_invalid=False,
            runner=lambda _prepared: _output(other),
        )
        result = execution.result
        self.assertIsNone(execution.output)
        self.assertEqual(result.disposition, SourceGenerationDisposition.FAILED)
        self.assertEqual(result.failure_code, "runner-output-identity-mismatch")

        def failed(_prepared):
            raise ComponentSourceGenerationRunError(
                "model-failed",
                runtime_observation=ComponentGenerationRuntimeObservation(
                    1, 20, 30, None
                ),
            )

        execution = execute_component_source_generation_node(
            node,
            candidate=None,
            explicitly_invalid=False,
            runner=failed,
        )
        result = execution.result
        self.assertEqual(result.failure_code, "model-failed")
        self.assertEqual(result.runtime_observation.model_attempts, 1)

        class AdapterFailure(RuntimeError):
            code = "source_generation.asset_authority_mismatch"

        def adapter_failed(_prepared):
            raise AdapterFailure("locked asset custody changed")

        execution = execute_component_source_generation_node(
            node,
            candidate=None,
            explicitly_invalid=False,
            runner=adapter_failed,
        )
        self.assertEqual(
            execution.result.failure_code,
            "source_generation.asset_authority_mismatch",
        )

        execution = execute_component_source_generation_node(
            node,
            candidate=None,
            explicitly_invalid=False,
            runner=lambda _prepared: (_ for _ in ()).throw(
                RuntimeError("uncoded runner failure")
            ),
        )
        self.assertEqual(execution.result.failure_code, "runner-failed")

        class InvalidAdapterFailure(RuntimeError):
            code = "INVALID CODE"

        execution = execute_component_source_generation_node(
            node,
            candidate=None,
            explicitly_invalid=False,
            runner=lambda _prepared: (_ for _ in ()).throw(
                InvalidAdapterFailure("untrusted diagnostic code")
            ),
        )
        self.assertEqual(execution.result.failure_code, "runner-failed")

    def test_schedule_uses_complete_nodes_and_canonical_results(self) -> None:
        execution = self.fixture.execution
        names = self.fixture.names
        candidates = {uri: _resume(node) for uri, node in self.fixture.by_uri.items()}
        changed = next(name for name in names.values() if name == "service")
        decision = _decision(execution, names, changed, (changed,))
        calls = []

        def runner(prepared):
            calls.append(prepared.definition.coordinate.name)
            return _output(prepared)

        result = schedule_source_generation(
            execution,
            invalidation=decision,
            prepared_nodes=self.fixture.by_uri,
            resume_candidates=candidates,
            runner=runner,
            max_parallelism=2,
        )
        self.assertEqual(calls, [changed])
        self.assertEqual(result.actual_generation_calls, 1)
        self.assertEqual(
            result, SourceGenerationScheduleResult.from_dict(result.to_dict())
        )
        with self.assertRaises(SourceGenerationSchedulingError) as raised:
            schedule_source_generation(
                execution,
                invalidation=decision,
                prepared_nodes=dict(tuple(self.fixture.by_uri.items())[1:]),
                resume_candidates=candidates,
                runner=runner,
            )
        self.assertEqual(
            raised.exception.code, "source_schedule.preparation_incomplete"
        )

    def test_contracts_are_cataloged_and_exclude_post_source_authority(self) -> None:
        node = self.fixture.nodes[0]
        resume = _resume(node)
        execution = execute_component_source_generation_node(
            node,
            candidate=resume,
            explicitly_invalid=False,
            runner=lambda prepared: _output(prepared),
        )
        result = execution.result
        self.assertEqual(execution.output, resume.output)
        schedule = SourceGenerationScheduleResult(
            self.fixture.execution.identity,
            canonical_identity({"invalidation": "fixture"}),
            1,
            (result,),
        )
        schemas = SchemaCatalog()
        documents = (resume.to_dict(), result.to_dict(), schedule.to_dict())
        for contract, document in zip(
            (resume, result, schedule), documents, strict=True
        ):
            with self.subTest(schema=contract.SCHEMA):
                schemas.validate(contract.SCHEMA, document)

        forbidden = {
            "authorization",
            "build_request",
            "provider_artifacts",
            "execution",
            "test_result",
            "acceptance",
            "promotion",
        }

        def keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | {
                    nested for item in value.values() for nested in keys(item)
                }
            if isinstance(value, list):
                return {nested for item in value for nested in keys(item)}
            return set()

        for document in documents:
            self.assertFalse(keys(document) & forbidden)
        parameters = inspect.signature(
            execute_component_source_generation_node
        ).parameters
        self.assertEqual(
            tuple(parameters),
            (
                "prepared",
                "candidate",
                "explicitly_invalid",
                "runner",
                "context_evidence_recorder",
            ),
        )

    def test_late_authorization_and_provider_state_cannot_change_generation_identity(
        self,
    ) -> None:
        node = self.fixture.nodes[0]
        resume = _resume(node)
        late_state = {
            "authorization": canonical_identity({"authorization": 1}),
            "provider_artifacts": canonical_identity({"provider": 1}),
        }
        first = execute_component_source_generation_node(
            node,
            candidate=resume,
            explicitly_invalid=False,
            runner=lambda prepared: _output(prepared),
        )
        late_state["authorization"] = canonical_identity({"authorization": 2})
        late_state["provider_artifacts"] = canonical_identity({"provider": 2})
        second = execute_component_source_generation_node(
            node,
            candidate=resume,
            explicitly_invalid=False,
            runner=lambda prepared: _output(prepared),
        )
        self.assertNotEqual(
            late_state["authorization"], canonical_identity({"authorization": 1})
        )
        self.assertEqual(first, second)
        self.assertEqual(first.result.identity, second.result.identity)

    def test_existing_v2_resume_contract_remains_distinct_and_readable(self) -> None:
        self.assertIsNot(
            SourceGenerationResumeCandidate,
            ComponentGenerationResumeCandidate,
        )
        self.assertEqual(
            ComponentGenerationResumeCandidate.SCHEMA,
            "urn:literate-ai:schema:v2:component-generation-resume-candidate",
        )


if __name__ == "__main__":
    unittest.main()
