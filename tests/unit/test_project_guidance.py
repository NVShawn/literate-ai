"""Conditional project guidance projection tests."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.application.project_guidance import project_guidance
from literate_ai.cli import main

ROOT = Path(__file__).resolve().parents[2]


class ProjectGuidanceTests(unittest.TestCase):
    def project(self, parent: Path, **policy_updates: object) -> Path:
        root = parent / "project"
        root.mkdir()
        manifest = json.loads(
            (ROOT / "literate.project.json").read_text(encoding="utf-8")
        )
        manifest["repository_policy"].update(policy_updates)
        (root / "literate.project.json").write_text(json.dumps(manifest))
        subprocess.run(("git", "init", "-q", str(root)), check=True)
        return root

    def test_develop_omits_disabled_optional_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(
                Path(directory),
                pull_request_labels=[],
                writers=[],
                main_state="free",
                pre_release_version=None,
            )
            result = project_guidance(root, operation="develop")

        facts = result["facts"]
        assert isinstance(facts, dict)
        configuration = facts["configuration"]
        assert isinstance(configuration, dict)
        self.assertNotIn("pull_request_labels", configuration)
        self.assertNotIn("writers", configuration)
        self.assertNotIn("pre_release_version", configuration)
        self.assertEqual(
            [item["id"] for item in result["requirements"]],
            ["development-posture"],
        )
        self.assertEqual(result["schema"], "literate-ai/project-guidance@1")
        self.assertRegex(result["identity"], r"^sha256:[0-9a-f]{64}$")

    def test_gc_is_concise_and_operation_specific(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(
                Path(directory), branch_gc_minimum_age_days=14, writers=[]
            )
            result = project_guidance(root, operation="gc")

        facts = result["facts"]
        assert isinstance(facts, dict)
        configuration = facts["configuration"]
        assert isinstance(configuration, dict)
        self.assertEqual(configuration["branch_gc_minimum_age_days"], 14)
        self.assertNotIn("merge_method", configuration)
        self.assertEqual(
            [item["id"] for item in result["requirements"]], ["minimum-age"]
        )
        self.assertEqual([item["id"] for item in result["argv"]], ["gc-plan"])

    def test_unsupported_forge_is_a_named_skip_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(Path(directory))
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(root),
                    "remote",
                    "add",
                    "origin",
                    "ssh://example.test/repo",
                ),
                check=True,
            )
            result = project_guidance(root, operation="land")

        self.assertEqual(result["argv"], [])
        self.assertEqual(result["skips"][0]["id"], "forge-unsupported")

    def test_cli_accepts_operation_before_optional_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(Path(directory), writers=[])
            output = io.StringIO()
            status = main(
                ("project", "guidance", "--operation", "gc", str(root)),
                stdout=output,
                stderr=io.StringIO(),
            )

        self.assertEqual(status, 0)
        envelope = json.loads(output.getvalue())
        self.assertEqual(envelope["result"]["operation"], "gc")

    def test_land_ci_status_is_bound_to_the_checkout_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(Path(directory), writers=[])
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(root),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/jordanhubbard/literate-ai.git",
                ),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(root), "add", "literate.project.json"),
                check=True,
            )
            environment = {
                "GIT_AUTHOR_NAME": "guidance-test",
                "GIT_AUTHOR_EMAIL": "guidance-test@example.com",
                "GIT_COMMITTER_NAME": "guidance-test",
                "GIT_COMMITTER_EMAIL": "guidance-test@example.com",
            }
            subprocess.run(
                ("git", "-C", str(root), "commit", "-q", "-m", "fixture"),
                check=True,
                env=environment,
            )
            revision = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            result = project_guidance(root, operation="land")

        facts = result["facts"]
        assert isinstance(facts, dict)
        runtime = facts["runtime"]
        assert isinstance(runtime, dict)
        git = runtime["git"]
        assert isinstance(git, dict)
        self.assertEqual(git["commit"], revision)
        commands = {item["id"]: item["argv"] for item in result["argv"]}
        self.assertEqual(commands["ci-status"][4], revision)


if __name__ == "__main__":
    unittest.main()
