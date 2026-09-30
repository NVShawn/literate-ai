"""Survey green reviews at cycle start and leftover Git work at cycle end."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.peer_work import (
    BRANCH_LIFECYCLE_SCHEMA,
    PEER_WORK_SCHEMA,
    classify_github_issue,
    classify_github_review,
    garbage_collect_peer_work,
    load_branch_lifecycle_markers,
    mark_branch_lifecycle,
    survey_peer_work,
)
from tests.unit.test_project_cli import invoke


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


class ClassifyGithubReviewTests(unittest.TestCase):
    def test_all_success_checks_are_green(self) -> None:
        classified = classify_github_review(
            {
                "number": 12,
                "title": "Ready",
                "headRefName": "feat/ready",
                "url": "https://github.com/org/repo/pull/12",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "statusCheckRollup": [
                    {"state": "SUCCESS"},
                    {"conclusion": "SKIPPED"},
                    {"state": "NEUTRAL"},
                ],
            }
        )
        self.assertEqual(classified["ci"], "green")
        self.assertFalse(classified["draft"])

    def test_review_preserves_release_and_coordination_fields(self) -> None:
        classified = classify_github_review(
            {
                "body": "Literate-AI-Release: 1.2",
                "baseRefName": "main",
                "headRefOid": "abc",
                "labels": [{"name": "release"}],
                "author": {"login": "alice"},
                "updatedAt": "2026-01-01T00:00:00Z",
            },
            {"main_state": "pre-release", "pre_release_version": "1.2.0"},
        )
        self.assertEqual(classified["release_relevance"], "current")
        self.assertEqual(classified["base"], "main")
        self.assertEqual(classified["head_revision"], "abc")
        self.assertEqual(classified["labels"], ["release"])
        self.assertEqual(classified["author"], "alice")

    def test_failure_is_red_and_empty_rollup_is_unknown(self) -> None:
        self.assertEqual(
            classify_github_review({"statusCheckRollup": [{"state": "FAILURE"}]})["ci"],
            "red",
        )
        self.assertEqual(
            classify_github_review({"statusCheckRollup": []})["ci"], "unknown"
        )
        self.assertEqual(
            classify_github_review({"statusCheckRollup": [{"state": "PENDING"}]})["ci"],
            "pending",
        )

    def test_completed_success_does_not_hide_unfinished_check_runs(self) -> None:
        for status in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"):
            for conclusion in (None, "", "SUCCESS"):
                with self.subTest(status=status, conclusion=conclusion):
                    classified = classify_github_review(
                        {
                            "statusCheckRollup": [
                                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                                {"status": status, "conclusion": conclusion},
                            ]
                        }
                    )
                    self.assertEqual(classified["ci"], "pending")

    def test_completed_success_does_not_hide_unknown_check_results(self) -> None:
        for check in (
            None,
            "invalid",
            {},
            {"status": "COMPLETED"},
            {"status": "NEW_STATE"},
            {"conclusion": 7},
        ):
            with self.subTest(check=check):
                self.assertEqual(
                    classify_github_review(
                        {
                            "statusCheckRollup": [
                                {"conclusion": "SUCCESS"},
                                check,
                            ]
                        }
                    )["ci"],
                    "unknown",
                )

    def test_failed_check_run_takes_precedence_over_pending(self) -> None:
        for conclusion in (
            "FAILURE",
            "ERROR",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
            "STALE",
        ):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(
                    classify_github_review(
                        {
                            "statusCheckRollup": [
                                {"status": "IN_PROGRESS", "conclusion": ""},
                                {"status": "COMPLETED", "conclusion": conclusion},
                            ]
                        }
                    )["ci"],
                    "red",
                )

    def test_issue_payload_keeps_number_title_and_url(self) -> None:
        classified = classify_github_issue(
            {
                "number": 7,
                "title": "Follow up",
                "url": "https://github.com/org/repo/issues/7",
            }
        )
        self.assertEqual(classified["number"], 7)
        self.assertEqual(classified["title"], "Follow up")


class SurveyPeerWorkTests(unittest.TestCase):
    def test_start_without_a_forge_has_no_green_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            result = survey_peer_work(root, when="start")
            self.assertEqual(result["schema"], PEER_WORK_SCHEMA)
            self.assertEqual(result["when"], "start")
            self.assertEqual(result["reviews"], [])
            self.assertEqual(result["green_reviews"], [])
            self.assertEqual(result["issues"], [])
            self.assertEqual(result["collectable_branches"], [])
            self.assertTrue(result["identity"].startswith("sha256:"))

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

    def test_end_omits_branches_already_merged_into_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "checkout", "-b", "already-landed")
            _commit(root, "landed work")
            _git(root, "checkout", "main")
            _git(root, "merge", "--no-ff", "-m", "merge landed", "already-landed")
            result = survey_peer_work(root, when="end")
            names = {item["name"] for item in result["collectable_branches"]}
            self.assertNotIn("already-landed", names)
            self.assertNotIn("main", names)
            terminal = {item["name"] for item in result["merged_terminal_candidates"]}
            self.assertIn("already-landed", terminal)

    def test_end_distinguishes_current_and_default_branch_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "branch", "already-landed")
            _git(root, "checkout", "-b", "feature")
            _commit(root, "feature work")
            _git(root, "checkout", "-b", "integration")
            _git(root, "checkout", "-b", "divergent", "main")
            _commit(root, "divergent work")
            _git(root, "checkout", "main")
            _commit(root, "trunk work")
            _git(root, "branch", "trunk-only")
            _git(root, "checkout", "integration")

            result = survey_peer_work(root, when="end")
            collectable = {
                item["name"]: item for item in result["collectable_branches"]
            }
            terminal = {
                item["name"]: item for item in result["merged_terminal_candidates"]
            }
            for name, current, default in (
                ("feature", True, False),
                ("divergent", False, False),
                ("already-landed", True, True),
                ("trunk-only", False, True),
            ):
                with self.subTest(branch=name):
                    item = (terminal if default else collectable)[name]
                    self.assertEqual(item["merged_into_current"], current)
                    self.assertEqual(item["merged_into_default"], default)
            self.assertNotIn("feature", terminal)
            with self.assertRaisesRegex(RuntimeError, "default branch"):
                mark_branch_lifecycle(
                    root, state="merged", branch="feature", actor="alice"
                )


class BranchLifecycleTests(unittest.TestCase):
    def test_marker_accepts_git_valid_underscore_prefixed_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "checkout", "-b", "_archive")
            _commit(root, "archive candidate")
            revision = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            _git(root, "checkout", "main")
            _git(root, "merge", "--no-ff", "-m", "land archive", "_archive")

            marker = mark_branch_lifecycle(
                root, state="merged", branch="_archive", actor="alice"
            )
            records, invalid = load_branch_lifecycle_markers(root)

            self.assertEqual(invalid, ())
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].branch, "_archive")
            self.assertEqual(records[0].head, revision)
            self.assertEqual(marker["branch"], "_archive")

    def test_marker_is_fetchable_and_readable_in_second_clone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = _init_repo(parent)
            remote = parent / "remote.git"
            _git(remote.parent, "init", "--bare", str(remote))
            _git(root, "remote", "add", "origin", str(remote))
            _git(root, "checkout", "-b", "landed")
            _commit(root, "landed")
            _git(root, "checkout", "main")
            _git(root, "merge", "--no-ff", "-m", "land", "landed")
            marker = mark_branch_lifecycle(
                root, state="merged", branch="landed", actor="alice", push=True
            )
            clone = parent / "clone"
            _git(parent, "clone", str(remote), str(clone))
            _git(clone, "fetch", "origin", f"{marker['ref']}:{marker['ref']}")
            completed = subprocess.run(
                ("git", "-C", str(clone), "cat-file", "blob", marker["ref"]),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(completed.stdout)["schema"], BRANCH_LIFECYCLE_SCHEMA
            )

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

    def test_dead_requires_reason_and_merged_requires_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "checkout", "-b", "unmerged")
            _commit(root, "unmerged")
            _git(root, "checkout", "main")
            with self.assertRaisesRegex(RuntimeError, "reason"):
                mark_branch_lifecycle(
                    root, state="dead", branch="unmerged", actor="alice"
                )
            with self.assertRaisesRegex(RuntimeError, "default branch"):
                mark_branch_lifecycle(
                    root, state="merged", branch="unmerged", actor="alice"
                )

    def test_gc_apply_requires_explicit_delete_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "authorize-delete"):
                garbage_collect_peer_work(root, apply=True)

    def test_gc_rejects_too_young_future_and_malformed_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "checkout", "-b", "landed")
            _commit(root, "landed")
            _git(root, "checkout", "main")
            _git(root, "merge", "--no-ff", "-m", "land", "landed")
            now = datetime(2026, 2, 1, tzinfo=UTC)
            marker = mark_branch_lifecycle(
                root,
                state="merged",
                branch="landed",
                actor="alice",
                clock=lambda: now,
            )
            young = garbage_collect_peer_work(
                root, clock=lambda: now + timedelta(days=7) - timedelta(microseconds=1)
            )
            self.assertIn("marker-too-young", young["rejected"][0]["reasons"])
            boundary = garbage_collect_peer_work(
                root, clock=lambda: now + timedelta(days=7)
            )
            self.assertEqual(boundary["candidates"][0]["branch"], "landed")
            future = garbage_collect_peer_work(
                root, clock=lambda: now - timedelta(seconds=1)
            )
            self.assertIn("future-marker", future["rejected"][0]["reasons"])
            malformed = (
                subprocess.run(
                    ("git", "-C", str(root), "hash-object", "-w", "--stdin"),
                    input=b'{"schema":"literate-ai/branch-lifecycle@1"}',
                    check=True,
                    capture_output=True,
                )
                .stdout.decode()
                .strip()
            )
            _git(root, "update-ref", marker["ref"], malformed)
            invalid = garbage_collect_peer_work(
                root, clock=lambda: now + timedelta(days=8)
            )
            self.assertIn("invalid-marker", invalid["rejected"][0]["reasons"])

    def test_gc_uses_repository_policy_minimum_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "checkout", "-b", "landed")
            _commit(root, "landed")
            _git(root, "checkout", "main")
            _git(root, "merge", "--no-ff", "-m", "land", "landed")
            marked_at = datetime(2026, 1, 1, tzinfo=UTC)
            policy = SimpleNamespace(
                branch_gc_minimum_age_days=30,
                default_branch="main",
                remote="origin",
            )
            with mock.patch(
                "literate_ai.adapters.peer_work._repository_policy",
                return_value=policy,
            ):
                mark_branch_lifecycle(
                    root,
                    state="merged",
                    branch="landed",
                    actor="alice",
                    clock=lambda: marked_at,
                )
                young = garbage_collect_peer_work(
                    root, clock=lambda: marked_at + timedelta(days=29)
                )
                old = garbage_collect_peer_work(
                    root, clock=lambda: marked_at + timedelta(days=30)
                )
            self.assertIn("marker-too-young", young["rejected"][0]["reasons"])
            self.assertEqual(old["candidates"][0]["branch"], "landed")

    def test_gc_apply_refuses_unknown_forge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "supported forge"):
                garbage_collect_peer_work(root, apply=True, authorize_delete=True)

    def test_gc_apply_refuses_failed_open_review_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "remote", "add", "origin", "https://github.com/org/repo.git")

            def unavailable(command, _root, _timeout):
                return subprocess.CompletedProcess(command, 1, "", "unavailable")

            with self.assertRaisesRegex(RuntimeError, "review-status command failed"):
                garbage_collect_peer_work(
                    root,
                    apply=True,
                    authorize_delete=True,
                    run_command=unavailable,
                )

    def test_marker_fields_are_bounded_and_branch_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "actor"):
                mark_branch_lifecycle(
                    root, state="merged", branch="main", actor="x" * 257
                )
            for branch in ("../main", "bad..name", "bad@{name}", "-option"):
                with self.subTest(branch=branch):
                    with self.assertRaisesRegex(RuntimeError, "safe Git branch"):
                        mark_branch_lifecycle(
                            root, state="merged", branch=branch, actor="alice"
                        )

    def test_marker_push_uses_configured_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = _init_repo(parent)
            remote = parent / "upstream.git"
            _git(parent, "init", "--bare", str(remote))
            _git(root, "remote", "add", "upstream", str(remote))
            policy = SimpleNamespace(remote="upstream")
            with mock.patch(
                "literate_ai.adapters.peer_work._repository_policy",
                return_value=policy,
            ):
                marker = mark_branch_lifecycle(
                    root, state="merged", branch="main", actor="alice", push=True
                )
            shown = subprocess.run(
                ("git", "-C", str(remote), "show-ref", "--verify", marker["ref"]),
                capture_output=True,
            )
            self.assertEqual(shown.returncode, 0)

    def test_dead_marker_can_bind_a_fetched_remote_only_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            _git(root, "remote", "add", "origin", "https://github.com/org/repo.git")
            _git(root, "checkout", "-b", "remote-only")
            _commit(root, "remote proposal")
            revision = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            _git(root, "checkout", "main")
            _git(
                root,
                "update-ref",
                "refs/remotes/origin/remote-only",
                revision,
            )
            _git(root, "update-ref", "-d", "refs/heads/remote-only")

            marker = mark_branch_lifecycle(
                root,
                state="dead",
                branch="remote-only",
                actor="release-engineer",
                reason="The proposal was incorporated through another commit.",
            )

            self.assertEqual(marker["head"], revision)
            self.assertEqual(marker["branch"], "remote-only")

    def test_gc_revalidates_stale_branch_before_deletion(self) -> None:
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
            _git(root, "remote", "add", "origin", "https://github.com/org/repo.git")
            calls = 0

            def race(command, _root, _timeout):
                nonlocal calls
                calls += 1
                if calls == 2:
                    _git(root, "update-ref", "refs/heads/landed", "refs/heads/main")
                return subprocess.CompletedProcess(command, 0, "[]", "")

            result = garbage_collect_peer_work(
                root,
                apply=True,
                authorize_delete=True,
                run_command=race,
                clock=lambda: marked_at + timedelta(days=8),
            )
            self.assertEqual(result["deleted"], [])
            self.assertIn("stale-marker", result["rejected"][0]["reasons"])
            self.assertTrue(_branch_exists(root, "landed"))

    def test_gc_rejects_stale_marker_and_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = _init_repo(parent)
            _git(root, "checkout", "-b", "old")
            _commit(root, "old")
            _git(root, "checkout", "main")
            mark_branch_lifecycle(
                root, state="dead", branch="old", actor="alice", reason="obsolete"
            )
            _git(root, "checkout", "old")
            _commit(root, "changed")
            _git(root, "checkout", "main")
            result = garbage_collect_peer_work(root)
            self.assertIn("stale-marker", result["rejected"][0]["reasons"])

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


class PeerWorkCliTests(unittest.TestCase):
    def test_start_survey_is_named_on_the_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _init_repo(Path(directory))
            status, envelope = invoke(
                "project", "peer-work", "--when", "start", str(root)
            )
            self.assertEqual(status, 0)
            result = envelope["result"]
            self.assertEqual(result["schema"], PEER_WORK_SCHEMA)
            self.assertEqual(result["when"], "start")
            self.assertEqual(result["green_reviews"], [])

    def test_when_is_required(self) -> None:
        status, envelope = invoke("project", "peer-work")
        self.assertEqual(status, 2)
        self.assertEqual(envelope["error"]["code"], "cli.usage")


if __name__ == "__main__":
    unittest.main()
