"""Survey green reviews at cycle start and leftover Git work at cycle end."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.peer_work import (
    garbage_collect_peer_work,
    mark_branch_lifecycle,
    survey_peer_work,
)


def _git(root: Path, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["GIT_AUTHOR_NAME"] = "peer-work-test"
    environment["GIT_AUTHOR_EMAIL"] = "peer-work-test@example.com"
    environment["GIT_COMMITTER_NAME"] = "peer-work-test"
    environment["GIT_COMMITTER_EMAIL"] = "peer-work-test@example.com"
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env=environment,
    )


def _commit(root: Path, message: str) -> None:
    (root / "README").write_text(message + "\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-m", message)


def _init_repo(directory: Path) -> Path:
    root = directory / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.name", "peer-work-test")
    _git(root, "config", "user.email", "peer-work-test@example.com")
    _commit(root, "initial")
    return root


class SurveyPeerWorkTests(unittest.TestCase):
    def test_start_classifies_green_reviews_and_lists_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(
                root,
                "remote",
                "add",
                "origin",
                "https://github.com/org/repo.git",
            )
            payloads = {
                ("gh", "pr"): [
                    {
                        "number": 2,
                        "title": "Red",
                        "headRefName": "feat/red",
                        "url": "https://github.com/org/repo/pull/2",
                        "isDraft": False,
                        "statusCheckRollup": [{"state": "FAILURE"}],
                    },
                    {
                        "number": 1,
                        "title": "Green",
                        "headRefName": "feat/green",
                        "url": "https://github.com/org/repo/pull/1",
                        "isDraft": False,
                        "statusCheckRollup": [{"state": "SUCCESS"}],
                    },
                    {
                        "number": 3,
                        "title": "Draft",
                        "headRefName": "feat/draft",
                        "url": "https://github.com/org/repo/pull/3",
                        "isDraft": True,
                        "statusCheckRollup": [{"state": "SUCCESS"}],
                    },
                ],
                ("gh", "issue"): [
                    {
                        "number": 9,
                        "title": "Ticket",
                        "url": "https://github.com/org/repo/issues/9",
                    }
                ],
            }

            def run_command(command, _root, _timeout):
                key = (command[0], command[1])
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(payloads[key]), ""
                )

            result = survey_peer_work(root, when="start", run_command=run_command)
            self.assertEqual([item["number"] for item in result["green_reviews"]], [1])
            self.assertEqual([item["number"] for item in result["reviews"]], [1, 3, 2])
            self.assertEqual(result["issues"][0]["number"], 9)
            self.assertEqual(result["collectable_worktrees"], [])

    def test_end_names_unmerged_branches_and_other_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = _init_repo(parent)
            leftover = parent / "leftover"
            _git(root, "checkout", "-b", "stale-branch")
            _commit(root, "stale work")
            _git(root, "checkout", "main")
            _git(root, "worktree", "add", "-b", "worktree-branch", str(leftover))
            _commit(leftover, "worktree work")
            result = survey_peer_work(root, when="end")
            self.assertEqual(result["when"], "end")
            self.assertEqual(result["green_reviews"], [])
            self.assertEqual(result["issues"], [])
            branch_names = {item["name"] for item in result["collectable_branches"]}
            self.assertIn("stale-branch", branch_names)
            self.assertIn("worktree-branch", branch_names)
            self.assertNotIn("main", branch_names)
            self.assertEqual(result["collectable_worktrees"], [])
            statuses = {item["path"]: item["status"] for item in result["worktrees"]}
            self.assertEqual(statuses[str(leftover.resolve())], "clean")


class BranchLifecycleTests(unittest.TestCase):
    def test_gc_is_dry_run_then_deletes_exact_safe_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "checkout", "-b", "landed")
            _commit(root, "landed")
            _git(root, "checkout", "main")
            _git(root, "merge", "--no-ff", "-m", "land", "landed")
            marked_at = datetime(2026, 1, 1, tzinfo=UTC)
            mark_branch_lifecycle(
                root,
                state="merged",
                branch="landed",
                actor="alice",
                clock=lambda: marked_at,
            )
            planned = garbage_collect_peer_work(
                root, clock=lambda: marked_at + timedelta(days=7)
            )
            self.assertEqual(
                [item["branch"] for item in planned["candidates"]], ["landed"]
            )
            self.assertEqual(_branch_exists(root, "landed"), True)
            _git(root, "remote", "add", "origin", "https://github.com/org/repo.git")

            def no_reviews(command, _root, _timeout):
                return subprocess.CompletedProcess(command, 0, "[]", "")

            applied = garbage_collect_peer_work(
                root,
                apply=True,
                authorize_delete=True,
                run_command=no_reviews,
                clock=lambda: marked_at + timedelta(days=7),
            )
            self.assertEqual(applied["deleted"], ["landed"])
            self.assertEqual(_branch_exists(root, "landed"), False)

    def test_gc_rejects_dirty_worktree_and_open_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = _init_repo(parent)
            _git(root, "remote", "add", "origin", "https://github.com/org/repo.git")
            worktree = parent / "peer"
            _git(root, "worktree", "add", "-b", "peer", str(worktree))
            _commit(worktree, "peer")
            mark_branch_lifecycle(
                root, state="dead", branch="peer", actor="alice", reason="abandoned"
            )
            (worktree / "DIRTY").write_text("dirty", encoding="utf-8")

            def run_command(command, _root, _timeout):
                payload = [{"headRefName": "peer", "statusCheckRollup": []}]
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

            result = garbage_collect_peer_work(root, run_command=run_command)
            reasons = result["rejected"][0]["reasons"]
            self.assertIn("dirty-worktree", reasons)
            self.assertIn("open-review", reasons)


def _branch_exists(root: Path, branch: str) -> bool:
    return (
        subprocess.run(
            ("git", "-C", str(root), "show-ref", "--verify", f"refs/heads/{branch}"),
            capture_output=True,
        ).returncode
        == 0
    )


if __name__ == "__main__":
    unittest.main()
