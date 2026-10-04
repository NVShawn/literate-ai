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
    classify_git_remote_url,
    classify_release_relevance,
    inspect_remotes,
    sanitize_git_url,
)
from literate_ai.cli import main
from tests.support.fixtures_test_project_cli import invoke

_PLAIN_TEXT_ENVIRONMENT = {
    key: value
    for key, value in os.environ.items()
    if key not in {"FORCE_COLOR", "COLORTERM", "CLICOLOR", "CLICOLOR_FORCE"}
} | {"NO_COLOR": "1", "TERM": "dumb"}


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
    def test_github_https_ssh_and_enterprise_hosts(self) -> None:
        for url in (
            "https://github.com/jordanhubbard/literate-ai.git",
            "git@github.com:jordanhubbard/literate-ai.git",
            "ssh://git@github.com/jordanhubbard/literate-ai.git",
            "https://github.example.com/org/repo.git",
            "git@gist.github.com:123.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(classify_git_remote_url(url), "github")

    def test_gitlab_saas_and_self_hosted_hosts(self) -> None:
        for url in (
            "https://gitlab.com/group/project.git",
            "git@gitlab.com:group/project.git",
            "git@gitlab.internal.example:org/repo.git",
            "https://gitlab.example.com/a/b.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(classify_git_remote_url(url), "gitlab")

    def test_unknown_and_ambiguous_hosts(self) -> None:
        self.assertEqual(
            classify_git_remote_url("https://bitbucket.org/a/b.git"), "unsupported"
        )
        self.assertEqual(classify_git_remote_url("/tmp/local.git"), "unsupported")
        self.assertEqual(
            classify_git_remote_url("https://github.gitlab.example.com/a/b.git"),
            "ambiguous",
        )
        self.assertEqual(
            classify_git_remote_url("https://gitlab.internal.example/org/repo.git"),
            "gitlab",
        )

    def test_sanitize_strips_userinfo(self) -> None:
        self.assertEqual(
            sanitize_git_url("https://user:token@github.com/org/repo.git"),
            "https://github.com/org/repo.git",
        )


class InspectRemotesTests(unittest.TestCase):
    def test_origin_github_names_gh_issue_and_pr_commands(self) -> None:
        revision = "a" * 40
        inspect = inspect_remotes(
            (
                GitRemote("origin", "https://github.com/org/repo.git"),
                GitRemote("gitlab", "https://gitlab.com/org/repo.git"),
            ),
            revision=revision,
        )
        self.assertEqual(inspect.forge, "github")
        self.assertEqual(inspect.cli, "gh")
        self.assertEqual(inspect.issue_list[0], "gh")
        self.assertEqual(inspect.review_list[:2], ("gh", "pr"))
        self.assertEqual(inspect.ci_status[:2], ("gh", "run"))
        self.assertEqual(inspect.ci_status[4], revision)
        self.assertEqual(inspect.land_create, ("gh", "pr", "create"))
        self.assertEqual(inspect.land_merge[:3], ("gh", "pr", "merge"))
        self.assertEqual(inspect.review_status[:3], ("gh", "pr", "list"))
        fields = inspect.review_status[inspect.review_status.index("--json") + 1]
        for field in (
            "body",
            "baseRefName",
            "headRefOid",
            "labels",
            "author",
            "updatedAt",
        ):
            self.assertIn(field, fields.split(","))
        self.assertEqual(inspect.issue_status[:3], ("gh", "issue", "list"))
        self.assertEqual(inspect.issue_search[:3], ("gh", "issue", "list"))
        self.assertIn("QUERY", inspect.issue_search)
        self.assertEqual(inspect.issue_create[:3], ("gh", "issue", "create"))
        self.assertIn("BODY_FILE", inspect.issue_create)
        self.assertEqual(inspect.review_noun, "pull-request")

    def test_github_ci_status_requires_an_exact_commit(self) -> None:
        remote = (GitRemote("origin", "https://github.com/org/repo.git"),)
        self.assertEqual(inspect_remotes(remote).ci_status, ())
        with self.assertRaisesRegex(ValueError, "lowercase 40- or 64-character"):
            inspect_remotes(remote, revision="HEAD")

    def test_sole_gitlab_remote_names_glab_issue_and_mr_commands(self) -> None:
        inspect = inspect_remotes(
            (GitRemote("internal", "git@gitlab.internal.example:org/repo.git"),)
        )
        self.assertEqual(inspect.forge, "gitlab")
        self.assertEqual(inspect.cli, "glab")
        self.assertEqual(inspect.review_list[:2], ("glab", "mr"))
        self.assertEqual(inspect.ci_status[:2], ("glab", "ci"))
        self.assertEqual(inspect.land_create, ("glab", "mr", "create"))
        self.assertEqual(inspect.issue_search[:3], ("glab", "issue", "list"))
        self.assertIn("QUERY", inspect.issue_search)
        self.assertEqual(inspect.issue_create[:3], ("glab", "issue", "create"))
        self.assertIn("BODY_FILE", inspect.issue_create)
        self.assertEqual(inspect.review_noun, "merge-request")

    def test_conflicting_remotes_without_origin_are_ambiguous(self) -> None:
        inspect = inspect_remotes(
            (
                GitRemote("gh", "https://github.com/org/a.git"),
                GitRemote("gl", "https://gitlab.com/org/b.git"),
            )
        )
        self.assertEqual(inspect.forge, "ambiguous")

    def test_release_trailer_is_exact_and_policy_relative(self) -> None:
        policy = {"main_state": "pre-release", "pre_release_version": "2.4.0-rc.1"}
        self.assertEqual(
            classify_release_relevance("Text\nLiterate-AI-Release: 2.4", policy),
            ("current", "2.4"),
        )
        self.assertEqual(
            classify_release_relevance("Literate-AI-Release: 2.3", policy),
            ("other", "2.3"),
        )
        self.assertEqual(
            classify_release_relevance("Literate-AI-Release: none", policy),
            ("unknown", "none"),
        )
        self.assertEqual(
            classify_release_relevance(
                "Literate-AI-Release: none",
                {"main_state": "free", "pre_release_version": None},
            ),
            ("none", "none"),
        )
        self.assertEqual(
            classify_release_relevance(" Literate-AI-Release: 2.4", policy),
            ("unknown", None),
        )
        self.assertEqual(
            classify_release_relevance(
                "Literate-AI-Release: 2.4",
                {"main_state": "free", "pre_release_version": "2.4"},
            ),
            ("unknown", "2.4"),
        )
        self.assertEqual(
            classify_release_relevance(
                "Literate-AI-Release: 2.4\nLiterate-AI-Release: nope", policy
            ),
            ("unknown", None),
        )
        self.assertEqual(
            classify_release_relevance("x Literate-AI-Release: 2.4", policy),
            ("unknown", None),
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

    def test_inspect_gitlab_self_hosted_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git(root, "init", "--quiet", "-b", "main")
            _git(
                root,
                "remote",
                "add",
                "origin",
                "git@gitlab.internal.example:org/derived.git",
            )
            status, envelope = invoke("project", "tracker", "inspect", str(root))
            self.assertEqual(status, 0)
            result = envelope["result"]
            self.assertEqual(result["forge"], "gitlab")
            self.assertEqual(result["cli"], "glab")
            self.assertEqual(result["review_list"][:2], ["glab", "mr"])

    def test_inspect_fails_closed_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status, envelope = invoke("project", "tracker", "inspect", directory)
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"], "project.tracker_not_a_git_repository"
            )

    def test_help_names_inspect(self) -> None:
        stdout = __import__("io").StringIO()
        stderr = __import__("io").StringIO()
        with mock.patch.dict(os.environ, _PLAIN_TEXT_ENVIRONMENT, clear=True):
            status = main(
                ("project", "tracker", "inspect", "help"),
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(status, 0)
        self.assertIn("usage: litai project tracker inspect", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
