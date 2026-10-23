"""GitHub vs GitLab tracker classification from Git remotes."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.application.project_tracker import (
    GitRemote,
    inspect_remotes,
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


class RepositoryCommandBindingTests(unittest.TestCase):
    def test_every_command_keeps_selected_fork_host_and_namespace(self) -> None:
        revision = "a" * 40
        for url, expected in (
            ("git@github.com:operator/fork.git", "github.com/operator/fork"),
            (
                "https://user:secret@github.enterprise.test/team/project.git",
                "github.enterprise.test/team/project",
            ),
            (
                "ssh://git@github.enterprise.test:2222/team/project.git",
                "github.enterprise.test/team/project",
            ),
            (
                "git@gitlab.example.test:group/subgroup/project.git",
                "https://gitlab.example.test/group/subgroup/project",
            ),
            (
                "https://user:secret@gitlab.example.test:8443/group/project.git",
                "https://gitlab.example.test:8443/group/project",
            ),
            (
                "http://gitlab.example.test:8080/group/project.git",
                "http://gitlab.example.test:8080/group/project",
            ),
        ):
            with (
                self.subTest(url=url),
                mock.patch.dict(
                    os.environ,
                    {
                        "GH_REPO": "upstream/other",
                        "GH_HOST": "github.other.test",
                        "GITLAB_HOST": "gitlab.other.test",
                    },
                ),
            ):
                result = inspect_remotes(
                    (
                        GitRemote("upstream", "https://github.com/upstream/parent"),
                        GitRemote("origin", url),
                    ),
                    revision=revision,
                ).to_dict()
                self.assertEqual(result["remote"], "origin")
                self.assertNotIn("secret", json.dumps(result))
                for field in (
                    "issue_list",
                    "review_list",
                    "issue_search",
                    "issue_create",
                    "ci_status",
                    "land_create",
                    "land_merge",
                    "review_status",
                    "issue_status",
                ):
                    command = result[field]
                    if command:
                        self.assertEqual(command[-2:], ["--repo", expected], field)
                        self.assertEqual(command.count("--repo"), 1, field)
                if result["forge"] == "github":
                    self.assertEqual(result["ci_status"][4], revision)

    def test_no_revision_still_omits_github_ci_query(self) -> None:
        result = inspect_remotes((GitRemote("only", "https://github.com/a/b"),))
        self.assertEqual(result.remote, "only")
        self.assertEqual(result.ci_status, ())
        self.assertEqual(result.issue_list[-2:], ("--repo", "github.com/a/b"))

    def test_invalid_repository_path_cannot_fall_back_to_ambient_repository(self):
        for url in (
            "https://github.com/owner",
            "https://github.com/owner/repo/extra",
            "https://github.com/owner/%2e%2e",
            "git@gitlab.example.test:group//repo.git",
        ):
            with self.subTest(url=url):
                result = inspect_remotes((GitRemote("origin", url),))
                self.assertIsNone(result.cli)
                self.assertFalse(result.issue_create)
                self.assertFalse(result.land_merge)


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
            self.assertEqual(
                result["issue_create"][-2:],
                ["--repo", "github.com/jordanhubbard/literate-ai"],
            )
            self.assertNotIn("token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
