"""Change-triggered NVIDIA SkillEvaluator admission tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.contracts.authoring_markdown import (
    AGENT_SKILL_EVALUATION_AUTHOR,
    render_authoring_markdown,
)
from scripts import evaluate_changed_skills


def _manifest(instructions: str = "Generate exact portable source.") -> bytes:
    return render_authoring_markdown(
        {
            "name": "portable-test-skill",
            "description": "Portable test skill. Use for test generation.",
            "metadata": {"author": AGENT_SKILL_EVALUATION_AUTHOR},
            "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
            "skill_id": "portable-test-skill",
            "version": "1.0.0",
            "title": "Portable Test Skill",
            "stages": ["generate"],
            "dependencies": [],
            "limitations": ["Do not invent behavior."],
            "trust": "reviewed",
        },
        f"# Portable Test Skill\n\n{instructions}",
    )


class SkillEvaluationGateTests(unittest.TestCase):
    def _repository(self, root: Path) -> Path:
        repository = root / "repository"
        skill = repository / "skills" / "specification-to-source" / "portable-test"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_bytes(_manifest())
        agent_skill = repository / "skills" / "agent" / "host-bootstrap"
        agent_skill.mkdir(parents=True)
        (agent_skill / "SKILL.md").write_text(
            "---\nname: host-bootstrap\ndescription: Bootstrap hosts.\n---\n",
            encoding="utf-8",
        )
        template = repository / "src" / "literate_ai" / "project_template"
        template.mkdir(parents=True)
        (template / "SKILL.md").write_text(
            "---\nname: literate-ai\ndescription: Derived project skill.\n---\n",
            encoding="utf-8",
        )
        subprocess.run(
            ("git", "init", str(repository)), check=True, capture_output=True
        )
        subprocess.run(
            ("git", "-C", str(repository), "config", "user.email", "test@example.com"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(repository), "config", "user.name", "Test"),
            check=True,
        )
        subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
        subprocess.run(
            ("git", "-C", str(repository), "commit", "-m", "fixture"),
            check=True,
            capture_output=True,
        )
        return repository

    def test_gate_skips_when_no_skill_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            with mock.patch.object(evaluate_changed_skills, "_evaluate") as evaluate:
                status = evaluate_changed_skills.main(["--repository", str(repository)])
        self.assertEqual(status, 0)
        evaluate.assert_not_called()

    def test_modified_typed_markdown_skill_is_copied_exactly_and_evaluated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            skill = repository / "skills" / "specification-to-source" / "portable-test"
            (skill / "SKILL.md").write_bytes(
                _manifest("Generate exact portable source and tests.")
            )
            canonical = (skill / "SKILL.md").read_bytes()
            observed = []

            def evaluate(executable: str, candidate: Path) -> None:
                observed.append((executable, candidate))
                candidate_bytes = (candidate / "SKILL.md").read_bytes()
                self.assertEqual(candidate_bytes, canonical)
                content = candidate_bytes.decode("utf-8")
                self.assertIn('name: "portable-test-skill"', content)
                self.assertIn("Generate exact portable source and tests.", content)

            with mock.patch.object(
                evaluate_changed_skills, "_evaluate", side_effect=evaluate
            ):
                status = evaluate_changed_skills.main(
                    [
                        "--repository",
                        str(repository),
                        "--evaluator",
                        "skillevaluator-fixture",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], "skillevaluator-fixture")


if __name__ == "__main__":
    unittest.main()
