"""Human-authored workflow Markdown and normalized wire projection tests."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.application.planning import (
    GENERATION_WORKFLOW_MARKDOWN_SCHEMA,
    GenerationPlanningError,
    normalize_generation_workflow_document,
)
from literate_ai.contracts import ProjectSourceGenerationCustody
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from scripts.migrate_workflow_authoring import migrate
from tests.support.fixtures_test_project_cli import copy_generation_catalogs, invoke

REPOSITORY = Path(__file__).resolve().parents[2]


class WorkflowMarkdownTests(unittest.TestCase):
    def test_repository_workflow_has_one_instruction_authority(self) -> None:
        path = REPOSITORY / "workflows" / "sample-host.md"
        metadata, body = parse_authoring_markdown(
            path.read_bytes(), source=path.as_posix()
        )
        self.assertEqual(metadata["schema"], GENERATION_WORKFLOW_MARKDOWN_SCHEMA)
        self.assertNotIn("instructions", json.dumps(metadata))
        self.assertIn("## Stage: plan", body)
        value = normalize_generation_workflow_document(path.read_bytes())
        stages = {item["stage_id"]: item for item in value["stages"]}
        self.assertIn("Plan the exact implementation", stages["plan"]["instructions"])
        self.assertEqual(stages["validate"]["instructions"], "")

    def test_legacy_json_migration_preserves_normalized_workflow(self) -> None:
        source = REPOSITORY / "workflows" / "sample-host.md"
        expected = normalize_generation_workflow_document(source.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "workflow.json"
            legacy.write_text(json.dumps(expected) + "\n", encoding="utf-8")
            migrated = migrate(legacy)
            self.assertEqual(
                normalize_generation_workflow_document(migrated.read_bytes()),
                expected,
            )
            self.assertFalse(legacy.exists())

    def test_frontmatter_instruction_and_unknown_body_stage_fail_closed(self) -> None:
        source = REPOSITORY / "workflows" / "sample-host.md"
        metadata, body = parse_authoring_markdown(
            source.read_bytes(), source=source.as_posix()
        )
        metadata["stages"][0]["instructions"] = "second authority"
        with self.assertRaises(GenerationPlanningError) as duplicate:
            normalize_generation_workflow_document(
                render_authoring_markdown(metadata, body)
            )
        self.assertEqual(
            duplicate.exception.code,
            "generation_plan.workflow_instruction_ambiguous",
        )

        metadata["stages"][0].pop("instructions")
        with self.assertRaises(GenerationPlanningError) as unknown:
            normalize_generation_workflow_document(
                render_authoring_markdown(
                    metadata, body + "\n\n## Stage: invented\n\nNo authority."
                )
            )
        self.assertEqual(
            unknown.exception.code, "generation_plan.workflow_instruction_unknown"
        )

    def test_project_rejects_markdown_and_json_for_the_same_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            status, result = invoke(
                "init",
                str(project),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0, result)
            markdown = (
                project / "workflows" / "production" / "staging" / "dev" / "workflow.md"
            )
            normalized = normalize_generation_workflow_document(
                markdown.read_bytes(), project_root=project, source=markdown
            )
            markdown.with_suffix(".json").write_text(
                json.dumps(normalized) + "\n", encoding="utf-8"
            )
            status, result = invoke("project", "validate", str(project))
            self.assertEqual(status, 2)
            self.assertEqual(
                result["error"]["code"], "project.workflow_authority_ambiguous"
            )


class NamedWorkflowCatalogTests(unittest.TestCase):
    """Nested workflow directories are independent catalog entries.

    Components select one by its ordinary catalog-relative path. Production is
    the outer workflow; staging and dev nest underneath so the filesystem is
    the scoping mechanism. Each pairs with ``routing.json`` in the matching
    directory, the same sentinel pattern as ``workflow.md`` and ``SKILL.md``.
    """

    WORKFLOWS = (
        ("dev", Path("production") / "staging" / "dev" / "workflow.md"),
        ("staging", Path("production") / "staging" / "workflow.md"),
        ("production", Path("production") / "workflow.md"),
    )
    ROUTING = (
        ("dev", Path("production") / "staging" / "dev" / "routing.json"),
        ("staging", Path("production") / "staging" / "routing.json"),
        ("production", Path("production") / "routing.json"),
    )

    def _prepare_model_routing_sample(self, project: Path) -> Path:
        status, result = invoke(
            "init", str(project), "--empty", "--flavor", "python", "--flavor", "macos"
        )
        self.assertEqual(status, 0, result)
        copy_generation_catalogs(project)
        for relative in (
            "workflows/production/staging/dev/workflow.md",
            "routing/production/staging/dev/routing.json",
        ):
            shutil.copy2(REPOSITORY / relative, project / relative)
        shutil.copytree(REPOSITORY / "flavors", project / "flavors", dirs_exist_ok=True)
        component = project / "samples" / "model-routing"
        shutil.copytree(REPOSITORY / "samples" / "model-routing", component)
        metadata, _body = parse_authoring_markdown(
            (component / "component.md").read_bytes(), source="model-routing"
        )
        self.assertEqual(
            metadata["workflow_definition"]["uri"],
            "workflows/production/staging/dev/workflow.md",
        )
        self.assertEqual(
            metadata["routing_policy"]["uri"],
            "routing/production/staging/dev/routing.json",
        )
        status, result = invoke(
            "project", "documentation-review", str(project), "--record"
        )
        self.assertEqual(status, 0, result)
        status, result = invoke("lock", str(component), "--target=host")
        self.assertEqual(status, 0, result)
        return component

    def _assert_nested_plan(self, component: Path) -> dict:
        status, envelope = invoke("plan", str(component), "--target=host")
        self.assertEqual(status, 0, envelope)
        plan = envelope["result"]
        self.assertEqual(
            plan["workflow"]["reference"]["uri"],
            "workflows/production/staging/dev/workflow.md",
        )
        self.assertEqual(
            plan["routing"]["reference"]["uri"],
            "routing/production/staging/dev/routing.json",
        )
        self.assertEqual(
            [stage["stage_id"] for stage in plan["workflow"]["model_stages"]],
            ["plan", "generate"],
        )
        return plan

    def test_real_sample_locks_and_plans_with_nested_catalog(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            project = Path(directory) / "project"
            component = self._prepare_model_routing_sample(project)
            self._assert_nested_plan(component)
            workflow = project / "workflows/production/staging/dev/workflow.md"
            workflow.write_bytes(
                workflow.read_bytes() + b"\nChanged workflow authority.\n"
            )
            status, result = invoke("plan", str(component), "--target=host")
            self.assertEqual(status, 2, result)
            self.assertEqual(result["error"]["code"], "component_lock.stale")

    @unittest.skipUnless(
        os.environ.get("LITERATE_AI_LIVE_SAMPLES") == "1",
        "set LITERATE_AI_LIVE_SAMPLES=1 to invoke the authenticated coding CLI",
    )
    def test_real_sample_locks_plans_and_generates_with_nested_catalog(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            project = Path(directory) / "project"
            component = self._prepare_model_routing_sample(project)
            plan = self._assert_nested_plan(component)
            output = project / "generated" / "model-routing"
            status, envelope = invoke(
                "generate", str(component), "--target=host", "--output", str(output)
            )
            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            custody = ProjectSourceGenerationCustody.from_dict(
                result["standard_source_generation"]
            )
            self.assertEqual(
                result["component_lock_identity"],
                plan["resolution"]["component_lock_identity"],
            )
            self.assertTrue(custody.successful)
            self.assertEqual(
                custody.root_revision.uri, plan["resolution"]["root_revision_identity"]
            )
            self.assertEqual(len(custody.components), 1)
            self.assertTrue(tuple(output.rglob("main.py")))

    def test_each_named_workflow_parses_and_declares_its_own_id(self) -> None:
        for name, relative in self.WORKFLOWS:
            with self.subTest(name=name):
                path = REPOSITORY / "workflows" / relative
                value = normalize_generation_workflow_document(
                    path.read_bytes(),
                    project_root=REPOSITORY,
                    source=path,
                )
                self.assertEqual(value["workflow_id"], name)
                stages = {item["stage_id"]: item for item in value["stages"]}
                self.assertTrue(stages["plan"]["instructions"])
                self.assertTrue(stages["generate"]["instructions"])
                if name != "dev":
                    self.assertIn("review", stages)
                    self.assertEqual(stages["generate"]["dependencies"], ["review"])

    def test_staging_and_production_tighten_the_generate_stage_token_budget(
        self,
    ) -> None:
        budgets = {}
        for name, relative in self.WORKFLOWS:
            path = REPOSITORY / "workflows" / relative
            value = normalize_generation_workflow_document(
                path.read_bytes(),
                project_root=REPOSITORY,
                source=path,
            )
            stages = {item["stage_id"]: item for item in value["stages"]}
            budgets[name] = stages["generate"]["maximum_output_tokens"]
        self.assertIsNone(budgets["dev"])
        self.assertEqual(budgets["staging"], 200000)
        self.assertEqual(budgets["production"], 120000)

    def test_each_named_workflow_pairs_with_a_distinctly_stricter_routing_policy(
        self,
    ) -> None:
        policies = {}
        for name, relative in self.ROUTING:
            path = REPOSITORY / "routing" / relative
            policies[name] = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                policies[name]["schema"], "literate-ai/generation-routing@1"
            )
        self.assertTrue(policies["dev"]["fallback_allowed"])
        self.assertFalse(policies["staging"]["fallback_allowed"])
        self.assertFalse(policies["production"]["fallback_allowed"])
        self.assertIsNone(policies["dev"]["required_locality"])
        self.assertIsNone(policies["staging"]["required_locality"])
        self.assertEqual(policies["production"]["required_locality"], "local")


if __name__ == "__main__":
    unittest.main()
