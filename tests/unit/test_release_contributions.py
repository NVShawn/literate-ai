from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.peer_work import mark_branch_lifecycle
from literate_ai.release_contributions import (
    ReleaseContributionsError,
    render_release_disposition,
    require_release_contributions_ready,
    sweep_release_contributions,
)


class ReleaseContributionsTests(unittest.TestCase):
    def git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def repository(self, parent: Path) -> Path:
        root = parent / "project"
        root.mkdir()
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "test@example.invalid")
        self.git(root, "config", "user.name", "Release Test")
        (root / "README.md").write_text("# Project\n", encoding="utf-8")
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "initial")
        self.git(root, "remote", "add", "origin", "https://github.com/org/project.git")
        self.git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        return root

    @staticmethod
    def completed(
        command: tuple[str, ...], value: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    def test_github_sweep_distinguishes_unclassified_included_and_deferred(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repository(Path(temporary))
            included = render_release_disposition(
                release="0.10.0",
                decision="include",
                milestone="0.10.0",
                reason="Release blocker.",
            )
            deferred = render_release_disposition(
                release="0.10.0",
                decision="defer",
                milestone="1.0",
                reason="Not release blocking.",
            )
            issues = [
                {
                    "number": 1,
                    "title": "blocking",
                    "url": "https://github.com/org/project/issues/1",
                    "updatedAt": "2026-09-04T00:00:00Z",
                    "milestone": {"title": "0.10.0"},
                    "comments": [{"body": included}],
                },
                {
                    "number": 2,
                    "title": "later",
                    "url": "https://github.com/org/project/issues/2",
                    "updatedAt": "2026-09-04T00:00:00Z",
                    "milestone": {"title": "1.0"},
                    "comments": [{"body": deferred}],
                },
                {
                    "number": 3,
                    "title": "unknown",
                    "url": "https://github.com/org/project/issues/3",
                    "updatedAt": "2026-09-04T00:00:00Z",
                    "milestone": None,
                    "comments": [],
                },
            ]

            def runner(command: tuple[str, ...], _cwd: Path, _timeout: int):
                if command[:3] == ("gh", "issue", "list"):
                    return self.completed(command, issues)
                if command[:3] == ("gh", "pr", "list"):
                    return self.completed(command, [])
                raise AssertionError(command)

            result = sweep_release_contributions(
                root,
                release="0.10.0",
                current_milestone="0.10.0",
                run_command=runner,
                refresh_remote=False,
            )
            self.assertFalse(result["ready"])
            self.assertEqual(
                result["in_scope_open"],
                [
                    {
                        "kind": "issue",
                        "number": 1,
                        "url": "https://github.com/org/project/issues/1",
                    }
                ],
            )
            self.assertEqual(result["unclassified"][0]["number"], 3)
            with self.assertRaises(ReleaseContributionsError) as blocked:
                require_release_contributions_ready(result)
            self.assertEqual(
                blocked.exception.code, "release.contributions_unclassified"
            )

    def merged_worktrees(self, parent: Path):
        root = self.repository(parent)
        topic, secondary = parent / "topic", parent / "secondary"
        self.git(root, "worktree", "add", "-b", "feature/merged", str(topic))
        (topic / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.git(topic, "add", ".")
        self.git(topic, "commit", "-m", "feature")
        self.git(root, "merge", "--ff-only", "feature/merged")
        self.git(root, "update-ref", "refs/remotes/origin/main", "main")
        self.git(root, "worktree", "add", "--detach", str(secondary), "main")
        return root, topic, secondary

    def empty_sweep(self, root: Path):
        return sweep_release_contributions(
            root,
            release="1.1.0",
            current_milestone="1.1",
            run_command=lambda command, _cwd, _timeout: self.completed(command, []),
            refresh_remote=False,
        )

    def test_merged_topic_worktree_is_resolved_from_a_secondary_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, topic, secondary = self.merged_worktrees(Path(temporary))
            revision = self.git(topic, "rev-parse", "HEAD")
            for invocation in (root, topic, secondary):
                with self.subTest(invocation=invocation.name):
                    result = self.empty_sweep(invocation)
                    self.assertTrue(result["ready"], result["unclassified"])
                    observed = next(
                        item
                        for item in result["worktrees"]
                        if item.get("branch") == "feature/merged"
                    )
                    self.assertEqual(observed["revision"], revision)
                    self.assertEqual(observed["reasons"], [])
            self.assertTrue(topic.is_dir())
            self.assertEqual(self.git(root, "rev-parse", "feature/merged"), revision)

    def test_fresh_clone_fetches_remote_branch_lifecycle_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            bare = parent / "remote.git"
            self.git(parent, "init", "--bare", str(bare))
            source_parent = parent / "source"
            source_parent.mkdir()
            source = self.repository(source_parent)
            self.git(source, "remote", "set-url", "origin", str(bare))
            self.git(source, "push", "-u", "origin", "main")
            self.git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
            self.git(source, "checkout", "-b", "feature/remote-dead")
            (source / "proposal.txt").write_text("proposal\n", encoding="utf-8")
            self.git(source, "add", ".")
            self.git(source, "commit", "-m", "superseded remote proposal")
            self.git(source, "push", "-u", "origin", "feature/remote-dead")
            self.git(source, "checkout", "main")
            mark_branch_lifecycle(
                source,
                state="dead",
                branch="feature/remote-dead",
                actor="release-engineer",
                reason="Behavior was incorporated by another reviewed implementation.",
                push=True,
            )
            clone = parent / "clone"
            self.git(parent, "clone", str(bare), str(clone))

            def runner(command: tuple[str, ...], _cwd: Path, _timeout: int):
                return self.completed(command, [])

            inspection = {
                "forge": "github",
                "url": "https://github.com/org/project.git",
                "git_root": str(clone),
            }
            with mock.patch(
                "literate_ai.release_contributions.inspect_project_tracker",
                return_value=inspection,
            ):
                result = sweep_release_contributions(
                    clone,
                    release="0.10.0",
                    current_milestone="0.10.0",
                    run_command=runner,
                )

            branch = next(
                item
                for item in result["branches"]
                if item["name"] == "feature/remote-dead"
            )
            self.assertEqual(branch["resolved_by"], "branch-lifecycle", msg=str(result))
            self.assertEqual(branch["lifecycle"]["state"], "dead")
            self.assertTrue(result["ready"])


if __name__ == "__main__":
    unittest.main()
