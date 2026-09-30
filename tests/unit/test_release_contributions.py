from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai import release_contributions as contributions
from literate_ai.adapters.peer_work import mark_branch_lifecycle
from literate_ai.release_contributions import (
    ReleaseContributionsError,
    disposition_release_contribution,
    parse_release_disposition,
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

    def repository(self, parent: Path, *, forge: str = "github") -> Path:
        root = parent / "project"
        root.mkdir()
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "test@example.invalid")
        self.git(root, "config", "user.name", "Release Test")
        (root / "README.md").write_text("# Project\n", encoding="utf-8")
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "initial")
        host = "github.com" if forge == "github" else "gitlab.example.com"
        self.git(root, "remote", "add", "origin", f"https://{host}/org/project.git")
        self.git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        return root

    @staticmethod
    def completed(
        command: tuple[str, ...], value: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    def test_disposition_marker_is_exact_and_last_valid_marker_wins(self) -> None:
        first = render_release_disposition(
            release="0.10.0",
            decision="include",
            milestone="0.10.0",
            reason="Required regression repair.",
        )
        second = render_release_disposition(
            release="0.10.0",
            decision="defer",
            milestone="1.0",
            reason="Reclassified after investigation.",
            branches=("feature/z", "feature/a", "feature/a"),
        )
        parsed = parse_release_disposition(first + "\n" + second, release="0.10.0")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["decision"], "defer")
        self.assertEqual(parsed["branches"], ["feature/a", "feature/z"])
        self.assertIsNone(parse_release_disposition(second, release="0.11.0"))

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

    def test_deferred_issue_can_disposition_an_unmerged_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repository(Path(temporary))
            self.git(root, "checkout", "-b", "feature/next")
            (root / "next.txt").write_text("next\n", encoding="utf-8")
            self.git(root, "add", ".")
            self.git(root, "commit", "-m", "future work")
            self.git(root, "checkout", "main")
            comment = render_release_disposition(
                release="0.10.0",
                decision="defer",
                milestone="1.0",
                reason="Scheduled for the next feature release.",
                branches=("feature/next",),
            )
            issues = [
                {
                    "number": 9,
                    "title": "future",
                    "url": "https://github.com/org/project/issues/9",
                    "updatedAt": "2026-09-04T00:00:00Z",
                    "milestone": {"title": "1.0"},
                    "comments": [{"body": comment}],
                }
            ]

            def runner(command: tuple[str, ...], _cwd: Path, _timeout: int):
                payload = issues if command[:3] == ("gh", "issue", "list") else []
                return self.completed(command, payload)

            result = sweep_release_contributions(
                root,
                release="0.10.0",
                current_milestone="0.10.0",
                run_command=runner,
                refresh_remote=False,
            )
            self.assertTrue(result["ready"])
            branch = next(
                item for item in result["branches"] if item["name"] == "feature/next"
            )
            self.assertEqual(branch["resolved_by"], "tracker-disposition")

    def test_linked_release_worktree_does_not_reclassify_default_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self.repository(parent)
            release = parent / "release"
            self.git(root, "worktree", "add", "-b", "release/0.10.x", str(release))

            def runner(command: tuple[str, ...], _cwd: Path, _timeout: int):
                return self.completed(command, [])

            result = sweep_release_contributions(
                release,
                release="0.10.1",
                current_milestone="0.10.1",
                run_command=runner,
                refresh_remote=False,
            )

            self.assertTrue(result["ready"], msg=str(result))
            default_worktree = next(
                item for item in result["worktrees"] if item.get("branch") == "main"
            )
            self.assertEqual(default_worktree["reasons"], [])

    def merged_worktrees(self, parent: Path, *, published: bool = True):
        root = self.repository(parent)
        topic, secondary = parent / "topic", parent / "secondary"
        self.git(root, "worktree", "add", "-b", "feature/merged", str(topic))
        (topic / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.git(topic, "add", ".")
        self.git(topic, "commit", "-m", "feature")
        self.git(root, "merge", "--ff-only", "feature/merged")
        if published:
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

    def test_merged_worktree_dirt_and_prunability_are_not_exempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, topic, secondary = self.merged_worktrees(Path(temporary))
            (topic / "foreign.txt").write_text("foreign\n", encoding="utf-8")
            result = self.empty_sweep(secondary)
            self.assertEqual(
                result["unclassified"][0]["reasons"], ["attached-worktree-is-dirty"]
            )
            topic.rename(Path(temporary) / "retained-topic")
            result = self.empty_sweep(secondary)
            self.assertIn(
                "attached-worktree-is-prunable", result["unclassified"][0]["reasons"]
            )

    def test_local_only_merge_is_not_remote_default_ancestry(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, secondary = self.merged_worktrees(Path(temporary), published=False)
            result = self.empty_sweep(secondary)
            self.assertFalse(result["ready"])
            self.assertTrue(
                any(
                    "attached-worktree-branch-is-unresolved" in item["reasons"]
                    for item in result["unclassified"]
                )
            )

    def test_stale_merged_marker_does_not_resolve_new_unmerged_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, topic, secondary = self.merged_worktrees(Path(temporary))
            mark_branch_lifecycle(
                root,
                state="merged",
                branch="feature/merged",
                actor="test",
                reason="Exact prior head was merged.",
            )
            self.git(topic, "commit", "--allow-empty", "-m", "new unmerged work")
            result = self.empty_sweep(secondary)
            self.assertFalse(result["ready"])
            self.assertTrue(
                any(
                    "attached-worktree-branch-is-unresolved" in item["reasons"]
                    for item in result["unclassified"]
                )
            )

    def test_branch_advance_after_merged_enumeration_is_not_resolved_by_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, topic, secondary = self.merged_worktrees(Path(temporary))
            real = contributions._git_lines

            def advance(root, *arguments):
                if arguments[:2] == ("worktree", "list"):
                    self.git(topic, "commit", "--allow-empty", "-m", "concurrent work")
                return real(root, *arguments)

            with mock.patch.object(contributions, "_git_lines", side_effect=advance):
                result = self.empty_sweep(secondary)
            self.assertFalse(result["ready"])
            self.assertTrue(
                any(
                    "attached-worktree-branch-is-unresolved" in item["reasons"]
                    for item in result["unclassified"]
                )
            )

    def test_worktree_head_or_branch_change_during_status_is_not_accepted(self):
        for change in ("commit", "branch"):
            with (
                tempfile.TemporaryDirectory() as temporary,
                self.subTest(change=change),
            ):
                _, topic, secondary = self.merged_worktrees(Path(temporary))
                real = contributions._git_lines

                def advance(root, *arguments, real=real, topic=topic, change=change):
                    result = real(root, *arguments)
                    if root.resolve() == topic.resolve() and arguments == (
                        "status",
                        "--porcelain",
                    ):
                        if change == "commit":
                            self.git(
                                topic, "commit", "--allow-empty", "-m", "concurrent"
                            )
                        else:
                            self.git(topic, "checkout", "-b", "feature/rebound")
                    return result

                with mock.patch.object(
                    contributions, "_git_lines", side_effect=advance
                ):
                    result = self.empty_sweep(secondary)
                self.assertFalse(result["ready"])
                self.assertTrue(
                    any(
                        "attached-worktree-changed" in item["reasons"]
                        for item in result["unclassified"]
                    )
                )

    def test_remote_default_ref_change_refuses_a_mixed_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _, secondary = self.merged_worktrees(Path(temporary))
            previous = self.git(root, "rev-parse", "HEAD^")
            real = contributions._git_lines

            def move_default(cwd, *arguments):
                result = real(cwd, *arguments)
                if any(arg.startswith("--merged=") for arg in arguments):
                    self.git(root, "update-ref", "refs/remotes/origin/main", previous)
                return result

            with mock.patch.object(
                contributions, "_git_lines", side_effect=move_default
            ):
                with self.assertRaises(ReleaseContributionsError) as caught:
                    self.empty_sweep(secondary)
            self.assertEqual(caught.exception.code, "release.contributions_git_changed")

    def test_remote_dead_marker_cannot_resolve_a_different_local_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, topic, secondary = self.merged_worktrees(
                Path(temporary), published=False
            )
            self.git(
                root,
                "update-ref",
                "refs/remotes/origin/feature/merged",
                "feature/merged",
            )
            mark_branch_lifecycle(
                root,
                state="dead",
                branch="feature/merged",
                actor="test",
                reason="Prior proposal superseded.",
            )
            self.git(topic, "commit", "--allow-empty", "-m", "new local proposal")
            result = self.empty_sweep(secondary)
            observed = next(
                item
                for item in result["worktrees"]
                if item.get("branch") == "feature/merged"
            )
            self.assertEqual(
                observed["reasons"], ["attached-worktree-branch-is-unresolved"]
            )

    def test_exact_dead_branch_marker_survives_tracker_issue_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repository(Path(temporary))
            self.git(root, "checkout", "-b", "feature/incorporated")
            (root / "incorporated.txt").write_text("proposal\n", encoding="utf-8")
            self.git(root, "add", ".")
            self.git(root, "commit", "-m", "superseded proposal")
            self.git(root, "checkout", "main")
            mark_branch_lifecycle(
                root,
                state="dead",
                branch="feature/incorporated",
                actor="release-engineer",
                reason="Behavior was incorporated through a different reviewed commit.",
            )

            def runner(command: tuple[str, ...], _cwd: Path, _timeout: int):
                return self.completed(command, [])

            result = sweep_release_contributions(
                root,
                release="0.10.0",
                current_milestone="0.10.0",
                run_command=runner,
                refresh_remote=False,
            )

            self.assertTrue(result["ready"])
            branch = next(
                item
                for item in result["branches"]
                if item["name"] == "feature/incorporated"
            )
            self.assertEqual(branch["resolved_by"], "branch-lifecycle", msg=str(result))
            self.assertEqual(branch["lifecycle"]["state"], "dead")

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

    def test_github_disposition_requires_authorization_and_uses_named_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repository(Path(temporary))
            with self.assertRaises(ReleaseContributionsError) as denied:
                disposition_release_contribution(
                    root,
                    kind="issue",
                    number=7,
                    release="0.10.0",
                    decision="defer",
                    milestone="1.0",
                    reason="Future work.",
                    authorize_external_write=False,
                )
            self.assertEqual(
                denied.exception.code, "release.external_authorization_required"
            )
            commands: list[tuple[str, ...]] = []

            def runner(command: tuple[str, ...], _cwd: Path, _timeout: int):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            disposition_release_contribution(
                root,
                kind="issue",
                number=7,
                release="0.10.0",
                decision="defer",
                milestone="1.0",
                reason="Future work.",
                authorize_external_write=True,
                run_command=runner,
            )
            self.assertEqual(commands[0][:3], ("gh", "issue", "edit"))
            self.assertEqual(commands[1][:3], ("gh", "issue", "comment"))
            self.assertIn(
                RELEASE_MARKER := "literate-ai-release-disposition:v1", commands[1][-1]
            )
            self.assertTrue(RELEASE_MARKER)

    def test_gitlab_sweep_reads_api_issues_reviews_and_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.repository(Path(temporary), forge="gitlab")
            comment = render_release_disposition(
                release="0.10.0",
                decision="defer",
                milestone="1.0",
                reason="Next release.",
            )

            def runner(command: tuple[str, ...], _cwd: Path, _timeout: int):
                endpoint = command[-1]
                if "/issues?" in endpoint:
                    value = [
                        {
                            "iid": 3,
                            "title": "later",
                            "web_url": "u",
                            "milestone": {"title": "1.0"},
                        }
                    ]
                elif "/merge_requests?" in endpoint:
                    value = []
                elif "/issues/3/notes?" in endpoint:
                    value = [{"id": 1, "body": comment}]
                else:
                    raise AssertionError(command)
                return self.completed(command, value)

            result = sweep_release_contributions(
                root,
                release="0.10.0",
                current_milestone="0.10.0",
                run_command=runner,
                refresh_remote=False,
            )
            self.assertTrue(result["ready"])
            self.assertEqual(result["issues"][0]["decision"], "defer")

    def test_gitlab_latest_disposition_wins_in_either_response_order(self):
        for decisions in (("include", "defer"), ("defer", "include")):
            for reverse in (False, True):
                with self.subTest(decisions=decisions, reverse=reverse):
                    notes = [
                        {
                            "id": index,
                            "body": render_release_disposition(
                                release="1.2.1",
                                decision=decision,
                                milestone="later" if decision == "defer" else "1.2.1",
                                reason="Updated release scope.",
                            ),
                        }
                        for index, decision in enumerate(decisions, start=1)
                    ]
                    if reverse:
                        notes.reverse()
                    result = contributions._gitlab_notes(
                        Path("."),
                        "org%2Fproject",
                        "issues",
                        3,
                        lambda command, *_, notes=notes: self.completed(command, notes),
                    )
                    latest = decisions[-1]
                    classified = contributions._classify_item(
                        {
                            "iid": 3,
                            "comments": result,
                            "milestone": {
                                "title": "later" if latest == "defer" else "1.2.1"
                            },
                        },
                        kind="issue",
                        release="1.2.1",
                        current_milestone="1.2.1",
                    )
                    self.assertEqual(classified["decision"], latest)
                    self.assertEqual(classified["reasons"], [])

    def test_gitlab_notes_fetches_every_page(self):
        calls = []

        def runner(command, *_):
            calls.append(command[-1])
            page = len(calls)
            return self.completed(
                command,
                [{"id": i, "body": "ordinary discussion"} for i in range(1, 101)]
                if page == 1
                else [{"id": 101, "body": "latest disposition"}],
            )

        result = contributions._gitlab_notes(
            Path("."),
            "org%2Fproject",
            "merge_requests",
            3,
            runner,
        )
        self.assertEqual(len(result), 101)
        self.assertEqual(result[-1]["body"], "latest disposition")
        self.assertEqual(len(calls), 2)
        self.assertIn("page=2", calls[1])

    def test_gitlab_notes_rejects_repeated_pages_and_invalid_identity(self):
        for notes in ([{"id": i} for i in range(1, 101)], [{"body": "missing id"}]):
            with self.subTest(notes=len(notes)):
                with self.assertRaises(ReleaseContributionsError):
                    contributions._gitlab_notes(
                        Path("."),
                        "org%2Fproject",
                        "issues",
                        3,
                        lambda command, *_, notes=notes: self.completed(command, notes),
                    )


if __name__ == "__main__":
    unittest.main()
