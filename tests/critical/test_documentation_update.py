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

    def test_rejects_secret_bearing_document_before_model_egress(self) -> None:
        secrets = (
            "api_key = abcdefghijklmnopqrstuvwxyz\n",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n",
            "OPENAI_API_KEY=abcdefghijklmnop\n",
            "Use sk-abcdefghijklmnopqrstuvwx for testing.\n",
        )
        for secret in secrets:
            with self.subTest(secret=secret), tempfile.TemporaryDirectory() as raw:
                project = self.project(Path(raw))
                guide = project.root / "docs" / "guide.md"
                guide.write_text(secret, encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
