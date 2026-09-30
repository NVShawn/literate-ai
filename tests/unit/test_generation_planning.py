"""Pinned workflow and routing content is executable generation input."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.application import (
    GenerationPlanningError,
    IndependentAcceptancePolicy,
    compile_generation_execution_plan,
)
from literate_ai.contracts import (
    Capability,
    ComponentDefinition,
    ContentIdentity,
    ContentReference,
    HashAlgorithm,
)
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.models import DataEgress, Locality, ModelEndpoint
from literate_ai.ports import NON_EXECUTING_EXACT_TREE_PROFILE

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = REPOSITORY_ROOT / "samples" / "hello-component"


def reference(kind: str, uri: str, content: bytes) -> ContentReference:
    return ContentReference(
        kind,
        uri,
        ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()),
    )


class GenerationPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        authoring_path = SAMPLE_ROOT / "component.md"
        authoring = parse_component_markdown(
            authoring_path,
            authoring_path.read_text(encoding="utf-8"),
            project_root=REPOSITORY_ROOT,
        )
        self.workflow = (
            REPOSITORY_ROOT / authoring.workflow_definition.uri
        ).read_bytes()
        self.routing = (REPOSITORY_ROOT / authoring.routing_policy.uri).read_bytes()
        self.definition = ComponentDefinition(
            coordinate=authoring.coordinate,
            version=authoring.version,
            display_name=authoring.display_name,
            description=authoring.description,
            profiles=authoring.profiles,
            sample=authoring.sample,
            provides=tuple(
                Capability(item.name, item.version, None) for item in authoring.provides
            ),
            requires=authoring.requires,
            specification_provider=authoring.specification_provider,
            specification_roots=authoring.specification_roots,
            authoring_inputs=(),
            workflow_definition=reference(
                "workflow", authoring.workflow_definition.uri, self.workflow
            ),
            routing_policy=reference(
                "routing-policy", authoring.routing_policy.uri, self.routing
            ),
            flavor_slots=authoring.flavor_slots,
            entrypoints=authoring.entrypoints,
            acceptance_contracts=(),
        )
        self.endpoint = ModelEndpoint(
            "fixture-coding-cli",
            "coding-cli-fixture",
            "fixture-model",
            "unix:///fixture-coding-cli",
            Locality.LOCAL,
            ("structured-output", "source-generation"),
            100_000,
        )

    def compile(
        self,
        definition: ComponentDefinition | None = None,
        *,
        workflow: bytes | None = None,
        routing: bytes | None = None,
    ):
        return compile_generation_execution_plan(
            component=definition or self.definition,
            workflow_content=workflow or self.workflow,
            routing_content=routing or self.routing,
            endpoint=self.endpoint,
            generation_prompt="Exact generated recipe sha256:fixture",
        )

    def workflow_dependencies(self, stage_id: str, dependencies: list[str]) -> bytes:
        metadata, body = parse_authoring_markdown(self.workflow, source="test workflow")
        stage = next(
            item for item in metadata["stages"] if item["stage_id"] == stage_id
        )
        stage["dependencies"] = dependencies
        return render_authoring_markdown(metadata, body)

    def test_exact_pins_compile_model_routes_and_guarded_lifecycle(self) -> None:
        plan = self.compile()
        self.assertEqual(
            [item.stage_id for item in plan.model_stages], ["plan", "generate"]
        )
        self.assertEqual(
            [item.dependencies for item in plan.model_stages],
            [(), ("plan",)],
        )
        self.assertEqual(
            plan.lifecycle_steps,
            (
                "validate",
                "classify",
                "authorize-build",
                "build",
                "resolve-dependencies",
                "test-generated",
                "verify-independent",
                "prepare-tree",
                "commit-tree",
            ),
        )
        self.assertTrue(
            all(
                item.data_egress is DataEgress.SOURCE_ALLOWED
                for item in plan.route_decisions
            )
        )
        self.assertEqual(plan.workflow_reference, self.definition.workflow_definition)
        self.assertEqual(plan.routing_reference, self.definition.routing_policy)

    def test_each_model_stage_can_bind_a_distinct_exact_endpoint(self) -> None:
        planner = ModelEndpoint(
            "deterministic-planner",
            "literate-ai/deterministic-planner",
            "planner-v1",
            "unix:///in-process/planner",
            Locality.LOCAL,
            ("structured-output",),
            10_000,
        )
        plan = compile_generation_execution_plan(
            component=self.definition,
            workflow_content=self.workflow,
            routing_content=self.routing,
            endpoint=self.endpoint,
            generation_prompt="Exact generated recipe sha256:fixture",
            stage_endpoints={"plan": planner},
        )
        self.assertEqual(
            [route.selected_endpoint_id for route in plan.route_decisions],
            ["deterministic-planner", "fixture-coding-cli"],
        )
        self.assertNotEqual(
            plan.route_decisions[0].selected_endpoint_digest,
            plan.route_decisions[1].selected_endpoint_digest,
        )

    def test_endpoint_override_cannot_target_an_unpinned_stage(self) -> None:
        with self.assertRaises(GenerationPlanningError) as caught:
            compile_generation_execution_plan(
                component=self.definition,
                workflow_content=self.workflow,
                routing_content=self.routing,
                endpoint=self.endpoint,
                generation_prompt="Exact generated recipe sha256:fixture",
                stage_endpoints={"invented": self.endpoint},
            )
        self.assertEqual(
            caught.exception.code, "generation_plan.endpoint_stage_unknown"
        )

    def test_stale_pin_is_rejected_before_any_route_can_execute(self) -> None:
        changed = self.routing.replace(b"source-allowed", b"none")
        with self.assertRaises(GenerationPlanningError) as caught:
            self.compile(routing=changed)
        self.assertEqual(caught.exception.code, "generation_plan.routing_policy_drift")

    def test_workflow_semantics_are_loaded_only_from_the_exact_pin(self) -> None:
        changed = self.workflow.replace(
            b"Plan the exact implementation",
            b"Map the exact implementation",
        )
        with self.assertRaises(GenerationPlanningError) as stale:
            self.compile(workflow=changed)
        self.assertEqual(stale.exception.code, "generation_plan.workflow_drift")

        changed_definition = replace(
            self.definition,
            workflow_definition=reference(
                "workflow", self.definition.workflow_definition.uri, changed
            ),
        )
        changed_plan = self.compile(changed_definition, workflow=changed)
        self.assertNotEqual(changed_plan.identity, self.compile().identity)
        self.assertIn(
            "Map the exact implementation", changed_plan.model_stages[0].instructions
        )

    def test_new_routing_pin_changes_the_executable_plan(self) -> None:
        baseline = self.compile()
        changed = self.routing.replace(b"source-allowed", b"none")
        changed_definition = replace(
            self.definition,
            routing_policy=reference(
                "routing-policy", self.definition.routing_policy.uri, changed
            ),
        )
        constrained = self.compile(changed_definition, routing=changed)
        self.assertNotEqual(constrained.identity, baseline.identity)
        self.assertTrue(
            all(
                item.data_egress is DataEgress.NONE
                for item in constrained.route_decisions
            )
        )

    def test_workflow_cannot_bypass_the_guarded_dependency_chain(self) -> None:
        changed = self.workflow_dependencies("validate", ["plan"])
        changed_definition = replace(
            self.definition,
            workflow_definition=reference(
                "workflow", self.definition.workflow_definition.uri, changed
            ),
        )
        with self.assertRaises(GenerationPlanningError) as caught:
            self.compile(changed_definition, workflow=changed)
        self.assertEqual(caught.exception.code, "generation_plan.unsupported_flow")

    def test_workflow_may_add_dependencies_without_changing_safety_order(self) -> None:
        changed = self.workflow_dependencies("classify", ["validate", "generate"])
        changed_definition = replace(
            self.definition,
            workflow_definition=reference(
                "workflow", self.definition.workflow_definition.uri, changed
            ),
        )
        plan = self.compile(changed_definition, workflow=changed)
        self.assertEqual(plan.lifecycle_steps[0:2], ("validate", "classify"))

    def test_acceptance_profile_is_bound_into_the_exact_plan(self) -> None:
        authorized = self.compile()
        suite_identity = "sha256:" + "a" * 64
        exact_tree = compile_generation_execution_plan(
            component=self.definition,
            workflow_content=self.workflow,
            routing_content=self.routing,
            endpoint=self.endpoint,
            generation_prompt="Exact generated recipe sha256:fixture",
            independent_acceptance_policy=(
                IndependentAcceptancePolicy.self_host_exact_tree(suite_identity)
            ),
        )

        self.assertNotEqual(authorized.identity, exact_tree.identity)
        self.assertEqual(
            exact_tree.independent_acceptance_policy.execution_profile,
            NON_EXECUTING_EXACT_TREE_PROFILE,
        )

    def test_non_executing_profile_cannot_name_a_generic_verifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved"):
            IndependentAcceptancePolicy(
                policy_id="fixture/non-executing@1",
                execution_profile=NON_EXECUTING_EXACT_TREE_PROFILE,
                runner_id="acceptance:generic@1",
                suite_identity="sha256:" + "a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
