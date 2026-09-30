"""Fail-closed documentation reconciliation tests."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.documentation_update import (
    DocumentationUpdateError,
    apply_documentation_update,
)
from literate_ai.projects import load_project


class ScriptedTaskRunner:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.prompt = ""

    def run_json_task(self, prompt: str, *, model: str | None = None):
        self.prompt = prompt
        return SimpleNamespace(
            response=self.response,
            request_identity="sha256:" + "1" * 64,
            response_identity="sha256:" + "2" * 64,
            selection_identity="sha256:" + "3" * 64,
            tool_binding_identity="sha256:" + "4" * 64,
        )


def identity(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class DocumentationUpdateTests(unittest.TestCase):
    def project(self, root: Path):
        (root / "docs").mkdir()
        (root / "docs" / "guide.md").write_text("# Old guide\n", encoding="utf-8")
        (root / "literate.project.json").write_text(
            """{
  "schema": "urn:literate-ai:schema:v2:project-definition",
  "project_id": "documentation-test",
  "version": "0.1.0",
  "profile": "canonical",
  "component_roots": ["components"],
  "flavor_roots": ["flavors"],
  "skill_roots": ["skills"],
  "workflow_roots": ["workflows"],
  "routing_roots": ["routing"],
  "documentation_roots": ["docs"],
  "agent_skill": "SKILL.md",
  "source_intelligence": {
    "schema": "urn:literate-ai:schema:v1:project-source-intelligence-policy",
    "provider_id": "none",
    "command": null,
    "minimum_version": null,
    "artifact_path": null,
    "stages": {
      "project-maintenance": "off",
      "source-generation": "off",
      "cache-consumption": "off",
      "source-to-specification": "off",
      "repository-source-admission": "off",
      "structural-review": "off"
    },
    "artifact_publication": "metadata-only"
  }
}
""",
            encoding="utf-8",
        )
        for directory in ("components", "flavors", "skills", "workflows", "routing"):
            (root / directory).mkdir()
        (root / "SKILL.md").write_text("# Agent\n", encoding="utf-8")
        return load_project(root)

    def proposal(self, path: str, before: bytes, content: str):
        return {
            "schema": "literate-ai/documentation-update-proposal@1",
            "changes": [
                {
                    "path": path,
                    "base_identity": identity(before),
                    "reasons": ["The guide is stale."],
                    "content": content,
                }
            ],
        }

    def apply(self, project, response):
        return apply_documentation_update(
            project,
            review={"state": "stale", "document": "docs/guide.md"},
            receipt_state="unconfigured",
            authority={"project_id": "documentation-test"},
            task_runner=ScriptedTaskRunner(response),
        )

    def test_applies_existing_markdown_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            result = self.apply(
                project,
                self.proposal("docs/guide.md", guide.read_bytes(), "# Current guide\n"),
            )
            self.assertTrue(result["applied"])
            self.assertEqual(guide.read_text(encoding="utf-8"), "# Current guide\n")
            self.assertEqual(result["marker"]["recorded"], False)

    def test_rolls_back_when_post_apply_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            runner = ScriptedTaskRunner(
                self.proposal("docs/guide.md", guide.read_bytes(), "# Broken guide\n")
            )

            def reject():
                raise ValueError("broken documentation graph")

            with self.assertRaisesRegex(DocumentationUpdateError, "could not validate"):
                apply_documentation_update(
                    project,
                    review={"state": "current", "document": "docs/guide.md"},
                    receipt_state="unconfigured",
                    authority={"project_id": "documentation-test"},
                    task_runner=runner,
                    validate_result=reject,
                )
            self.assertEqual(guide.read_text(encoding="utf-8"), "# Old guide\n")

    def test_rejects_malformed_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            with self.assertRaisesRegex(DocumentationUpdateError, "invalid schema"):
                self.apply(project, {"schema": "wrong", "changes": []})

    def test_rejects_review_marker_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            response = self.proposal(
                "docs/guide.md",
                guide.read_bytes(),
                "# Changed\n<!-- literate-ai:authority-review-pending -->\n",
            )
            with self.assertRaisesRegex(DocumentationUpdateError, "review markers"):
                self.apply(project, response)
            self.assertEqual(guide.read_text(encoding="utf-8"), "# Old guide\n")

    def test_rolls_back_when_final_plan_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            findings = "\n".join(
                f"Run `litai missing-command-{index}`." for index in range(1_001)
            )
            with self.assertRaisesRegex(DocumentationUpdateError, "too many findings"):
                self.apply(
                    project,
                    self.proposal(
                        "docs/guide.md", guide.read_bytes(), f"# Changed\n{findings}\n"
                    ),
                )
            self.assertEqual(guide.read_text(encoding="utf-8"), "# Old guide\n")

    def test_rejects_out_of_scope_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            response = self.proposal("SKILL.md", guide.read_bytes(), "# Changed\n")
            with self.assertRaisesRegex(DocumentationUpdateError, "outside existing"):
                self.apply(project, response)
            self.assertEqual(guide.read_text(encoding="utf-8"), "# Old guide\n")

    def test_rejects_authority_change_during_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            component = project.root / "components" / "router"
            component.mkdir()
            authority = component / "component.md"
            authority.write_text("# Initial authority\n", encoding="utf-8")
            runner = ScriptedTaskRunner(
                self.proposal("docs/guide.md", guide.read_bytes(), "# Changed\n")
            )
            original_run = runner.run_json_task

            def mutate(prompt: str, *, model: str | None = None):
                result = original_run(prompt, model=model)
                authority.write_text("# Concurrent authority\n", encoding="utf-8")
                return result

            runner.run_json_task = mutate
            with self.assertRaisesRegex(
                DocumentationUpdateError, "authority or documentation changed"
            ):
                apply_documentation_update(
                    project,
                    review={"state": "stale", "document": "docs/guide.md"},
                    receipt_state="unconfigured",
                    authority={"project_id": "documentation-test"},
                    task_runner=runner,
                )
            self.assertEqual(guide.read_text(encoding="utf-8"), "# Old guide\n")

    def test_rejects_stale_base_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            response = self.proposal("docs/guide.md", b"not current", "# Changed\n")
            with self.assertRaisesRegex(
                DocumentationUpdateError, "not based on current"
            ):
                self.apply(project, response)
            self.assertEqual(guide.read_text(encoding="utf-8"), "# Old guide\n")

    def test_prompt_includes_component_authority_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            component = project.root / "components" / "router"
            component.mkdir()
            (component / "component.md").write_text(
                "# Router authority\n\nSupports streaming.\n", encoding="utf-8"
            )
            runner = ScriptedTaskRunner(
                {
                    "schema": "literate-ai/documentation-update-proposal@1",
                    "changes": [],
                }
            )
            apply_documentation_update(
                project,
                review={"state": "stale", "document": "docs/guide.md"},
                receipt_state="unconfigured",
                authority={"project_id": "documentation-test"},
                task_runner=runner,
            )
            self.assertIn("Supports streaming.", runner.prompt)
            self.assertIn("components/router/component.md", runner.prompt)

    def test_rejects_secret_bearing_document_before_model_egress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            guide.write_text("api_key = abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
            runner = ScriptedTaskRunner(
                {
                    "schema": "literate-ai/documentation-update-proposal@1",
                    "changes": [],
                }
            )
            with self.assertRaisesRegex(DocumentationUpdateError, "cannot be sent"):
                apply_documentation_update(
                    project,
                    review={"state": "stale", "document": "docs/guide.md"},
                    receipt_state="unconfigured",
                    authority={"project_id": "documentation-test"},
                    task_runner=runner,
                )
            self.assertEqual(runner.prompt, "")

    def test_rejects_bearer_token_before_model_egress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            guide.write_text(
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n",
                encoding="utf-8",
            )
            runner = ScriptedTaskRunner(
                {
                    "schema": "literate-ai/documentation-update-proposal@1",
                    "changes": [],
                }
            )
            with self.assertRaisesRegex(DocumentationUpdateError, "cannot be sent"):
                apply_documentation_update(
                    project,
                    review={"state": "stale", "document": "docs/guide.md"},
                    receipt_state="unconfigured",
                    authority={"project_id": "documentation-test"},
                    task_runner=runner,
                )
            self.assertEqual(runner.prompt, "")

    def test_rejects_prefixed_secret_before_model_egress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            guide.write_text("OPENAI_API_KEY=abcdefghijklmnop\n", encoding="utf-8")
            runner = ScriptedTaskRunner(
                {
                    "schema": "literate-ai/documentation-update-proposal@1",
                    "changes": [],
                }
            )
            with self.assertRaisesRegex(DocumentationUpdateError, "cannot be sent"):
                apply_documentation_update(
                    project,
                    review={"state": "stale", "document": "docs/guide.md"},
                    receipt_state="unconfigured",
                    authority={"project_id": "documentation-test"},
                    task_runner=runner,
                )
            self.assertEqual(runner.prompt, "")

    def test_rejects_standalone_provider_token_before_model_egress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            guide.write_text(
                "Use sk-abcdefghijklmnopqrstuvwx for testing.\n", encoding="utf-8"
            )
            runner = ScriptedTaskRunner(
                {
                    "schema": "literate-ai/documentation-update-proposal@1",
                    "changes": [],
                }
            )
            with self.assertRaisesRegex(DocumentationUpdateError, "cannot be sent"):
                apply_documentation_update(
                    project,
                    review={"state": "stale", "document": "docs/guide.md"},
                    receipt_state="unconfigured",
                    authority={"project_id": "documentation-test"},
                    task_runner=runner,
                )
            self.assertEqual(runner.prompt, "")

    def test_rejects_secret_like_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            guide = project.root / "docs" / "guide.md"
            response = self.proposal(
                "docs/guide.md",
                guide.read_bytes(),
                "api_key = abcdefghijklmnopqrstuvwxyz\n",
            )
            with self.assertRaisesRegex(DocumentationUpdateError, "secret material"):
                self.apply(project, response)
            self.assertEqual(guide.read_text(encoding="utf-8"), "# Old guide\n")


if __name__ == "__main__":
    unittest.main()
