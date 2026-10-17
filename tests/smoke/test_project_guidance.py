"""Conditional project guidance projection tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.application.project_guidance import project_guidance

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

    def test_land_review_create_names_the_policy_target_branch(self) -> None:
        for url, expected in (
            (
                "https://github.com/example/project.git",
                ["gh", "pr", "create", "--base", "trunk"],
            ),
            (
                "https://gitlab.com/example/project.git",
                ["glab", "mr", "create", "--target-branch", "trunk"],
            ),
        ):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                root = self.project(
                    Path(directory),
                    writers=[],
                    default_branch="trunk",
                    pull_request_labels=[],
                )
                subprocess.run(
                    ("git", "-C", str(root), "remote", "add", "origin", url),
                    check=True,
                )
                result = project_guidance(root, operation="land")
                commands = {item["id"]: item["argv"] for item in result["argv"]}
                self.assertEqual(commands["review-create"], expected)


if __name__ == "__main__":
    unittest.main()
