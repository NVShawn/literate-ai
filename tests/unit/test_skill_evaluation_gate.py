"""Change-triggered NVIDIA SkillEvaluator admission tests."""

from __future__ import annotations

import io
import shutil
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
    def _orchestration_templates(self, repository: Path) -> Path:
        source = Path(__file__).resolve().parents[2]
        relative = evaluate_changed_skills.ORCHESTRATION_TEMPLATES
        target = repository / relative
        shutil.copytree(source / relative, target)
        return target

    def _commit(self, repository: Path) -> str:
        subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
        subprocess.run(
            ("git", "-C", str(repository), "commit", "-m", "templates"),
            check=True,
            capture_output=True,
        )
        return subprocess.check_output(
            ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
        ).strip()

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

    def test_root_agent_skill_and_untracked_native_skill_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            (repository / "SKILL.md").write_text(
                "---\nname: literate-ai\ndescription: Use Literate AI.\n---\n",
                encoding="utf-8",
            )
            extra = repository / "skills" / "source-to-specification" / "new-skill"
            extra.mkdir(parents=True)
            (extra / "SKILL.md").write_bytes(
                _manifest().replace(b"portable-test-skill", b"new-skill")
            )

            changed = evaluate_changed_skills.changed_skills(repository)

        self.assertEqual(
            tuple(path.relative_to(repository).as_posix() or "." for path in changed),
            (
                ".",
                "skills/agent/host-bootstrap",
                "skills/source-to-specification/new-skill",
                "skills/specification-to-source/portable-test",
            ),
        )

    def test_modified_derived_project_skill_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            skill = repository / "src" / "literate_ai" / "project_template"
            (skill / "SKILL.md").write_text(
                "---\nname: literate-ai\ndescription: Updated project skill.\n---\n",
                encoding="utf-8",
            )

            changed = evaluate_changed_skills.changed_skills(repository)

        self.assertEqual(
            tuple(path.relative_to(repository).as_posix() for path in changed),
            ("src/literate_ai/project_template",),
        )

    def test_agent_skill_resources_are_detected_and_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            skill = repository / "skills" / "agent" / "host-bootstrap"
            script = skill / "scripts" / "bootstrap.py"
            script.parent.mkdir()
            script.write_text("print('bootstrap')\n", encoding="utf-8")
            observed: list[Path] = []

            def evaluate(_executable: str, candidate: Path) -> None:
                observed.append(candidate)
                self.assertEqual(
                    (candidate / "scripts" / "bootstrap.py").read_text(
                        encoding="utf-8"
                    ),
                    "print('bootstrap')\n",
                )

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

    def test_generated_views_use_exact_selected_checkout_template_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            templates = self._orchestration_templates(repository)
            for resource in templates.glob("*.md"):
                canonical = resource.read_bytes().replace(b"\r\n", b"\n")
                resource.write_bytes(canonical.replace(b"\n", b"\r\n"))
            onboarding = templates / "onboarding.md"
            onboarding.write_text(
                onboarding.read_text(encoding="utf-8")
                + "\nSelected checkout admission marker.\n",
                encoding="utf-8",
            )
            expected = {
                onboarding.read_text(encoding="utf-8").encode("utf-8"),
                (templates / "agent.md").read_text(encoding="utf-8").encode("utf-8"),
            }
            observed = []

            def evaluate(executable, candidate):
                self.assertEqual(executable, "fixture-evaluator")
                if "orchestration" in candidate.parts:
                    observed.append((candidate / "SKILL.md").read_bytes())

            with mock.patch.object(
                evaluate_changed_skills, "_evaluate", side_effect=evaluate
            ):
                status = evaluate_changed_skills.main(
                    [
                        "--repository",
                        str(repository),
                        "--evaluator",
                        "fixture-evaluator",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(len(observed), 2)
            self.assertEqual(set(observed), expected)

    def test_generated_view_discovery_is_read_only_and_not_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            self._orchestration_templates(repository)
            output = io.StringIO()
            with (
                mock.patch("sys.stdout", output),
                mock.patch.object(
                    evaluate_changed_skills.tempfile,
                    "TemporaryDirectory",
                    side_effect=AssertionError("discovery must not stage"),
                ),
                mock.patch.object(evaluate_changed_skills, "_evaluate") as evaluate,
            ):
                status = evaluate_changed_skills.main(
                    ["--repository", str(repository), "--list"]
                )
            self.assertEqual(status, 0)
            self.assertIn("orchestration (generated skill views)", output.getvalue())
            evaluate.assert_not_called()

    def test_generated_view_selection_covers_committed_renderer_and_template_changes(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            templates = self._orchestration_templates(repository)
            baseline = self._commit(repository)
            changed = evaluate_changed_skills.orchestration_views_changed
            self.assertFalse(changed(repository))
            self.assertTrue(changed(repository, all_skills=True))
            (repository / "unrelated.txt").write_text("unrelated", encoding="utf-8")
            self.assertFalse(changed(repository))
            for relative in evaluate_changed_skills._ORCHESTRATION_RENDERERS:
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# rendering change\n", encoding="utf-8")
                self.assertTrue(changed(repository))
                path.unlink()
            for resource in sorted(templates.glob("*.md")):
                original = resource.read_bytes()
                resource.write_bytes(original + b"\n")
                self.assertTrue(changed(repository))
                resource.write_bytes(original)
            agent = templates / "agent.md"
            agent.write_bytes(agent.read_bytes() + b"\n")
            self._commit(repository)
            self.assertFalse(changed(repository))
            self.assertTrue(changed(repository, base=baseline))

    def test_deleted_templates_trigger_and_fail_instead_of_skipping(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            templates = self._orchestration_templates(repository)
            self._commit(repository)
            for path in templates.iterdir():
                path.unlink()
            templates.rmdir()
            self.assertTrue(
                evaluate_changed_skills.orchestration_views_changed(repository)
            )
            with (
                mock.patch.object(evaluate_changed_skills, "_evaluate"),
                self.assertRaises(FileNotFoundError),
            ):
                evaluate_changed_skills.main(
                    [
                        "--repository",
                        str(repository),
                        "--evaluator",
                        "fixture-evaluator",
                    ]
                )

    def test_generated_view_evaluator_failure_propagates(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            self._orchestration_templates(repository)

            def evaluate(_executable, candidate):
                if "orchestration" in candidate.parts:
                    raise RuntimeError("fixture generated skill rejected")

            with (
                mock.patch.object(
                    evaluate_changed_skills, "_evaluate", side_effect=evaluate
                ),
                self.assertRaisesRegex(RuntimeError, "generated skill rejected"),
            ):
                evaluate_changed_skills.main(
                    [
                        "--repository",
                        str(repository),
                        "--evaluator",
                        "fixture-evaluator",
                    ]
                )

    def test_all_mode_requires_templates_when_renderer_is_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            renderer = repository / "src/literate_ai/adapters/orchestration_scaffold.py"
            renderer.parent.mkdir(parents=True)
            renderer.write_text("# fixture renderer\n", encoding="utf-8")
            self.assertTrue(
                evaluate_changed_skills.orchestration_views_changed(
                    repository, all_skills=True
                )
            )
            with (
                mock.patch.object(evaluate_changed_skills, "_evaluate"),
                self.assertRaises(FileNotFoundError),
            ):
                evaluate_changed_skills.main(
                    ["--repository", str(repository), "--all", "--evaluator", "fixture"]
                )

    def test_evaluator_invocation_keeps_all_six_checks_and_disables_models(self):
        with mock.patch.object(
            evaluate_changed_skills.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ) as run:
            evaluate_changed_skills._evaluate("fixture-evaluator", Path("view"))
        self.assertEqual(
            run.call_args.args[0],
            (
                "fixture-evaluator",
                "validate",
                "view",
                "--checks",
                "schema,pii,license,quality,unicode,lint",
                "--no-llm",
                "--no-dedup",
            ),
        )


if __name__ == "__main__":
    unittest.main()
