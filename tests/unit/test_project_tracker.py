"""GitHub vs GitLab tracker classification from Git remotes."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.application.project_tracker import (
    sanitize_git_url,
)
from tests.support.fixtures_test_project_cli import invoke


def _git(root: Path, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["GIT_AUTHOR_NAME"] = "tracker-test"
    environment["GIT_AUTHOR_EMAIL"] = "tracker-test@example.com"
    environment["GIT_COMMITTER_NAME"] = "tracker-test"
    environment["GIT_COMMITTER_EMAIL"] = "tracker-test@example.com"
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env=environment,
    )


class ClassifyRemoteUrlTests(unittest.TestCase):
    def test_sanitize_strips_userinfo(self) -> None:
        self.assertEqual(
            sanitize_git_url("https://user:token@github.com/org/repo.git"),
            "https://github.com/org/repo.git",
        )


class TrackerInspectCliTests(unittest.TestCase):
    def test_inspect_reads_origin_from_git_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git(root, "init", "--quiet", "-b", "main")
            _git(
                root,
                "remote",
                "add",
                "origin",
                "https://github.com/jordanhubbard/literate-ai.git",
            )
            (root / "README.md").write_text("tracker fixture\n", encoding="utf-8")
            _git(root, "add", "README.md")
            _git(root, "commit", "-q", "-m", "tracker fixture")
            revision = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            status, envelope = invoke("project", "tracker", "inspect", str(root))
            self.assertEqual(status, 0)
            result = envelope["result"]
            self.assertEqual(result["forge"], "github")
            self.assertEqual(result["cli"], "gh")
            self.assertEqual(result["remote"], "origin")
            self.assertEqual(result["issue_list"][0], "gh")
            self.assertEqual(result["ci_status"][4], revision)
            self.assertNotIn("token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
