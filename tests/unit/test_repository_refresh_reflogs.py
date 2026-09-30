"""Native Git log policy, exact inert append bytes and refusal boundaries."""

from __future__ import annotations

import os
import subprocess
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters import repository_refresh_metadata as metadata
from literate_ai.adapters import repository_refresh_reflogs as reflogs
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.unit import test_repository_refresh_refs as fixtures
from tests.unit.test_repository_orchestration import git, repository, snapshot

IDENT = b"Test <test@example.test> 1700000000 +0000"


class RefreshReflogTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefreshReferenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def target(self, root):
        return (
            git(root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "next")
            .decode()
            .strip()
        )

    def observe(self, root=None, target=None, owner=None):
        root = root or self.root
        observed = self.fixture.observe(root)
        request = RepositoryRefreshRequest(
            (RepositoryRefreshTarget(root.name, target or self.target(root)),)
        )
        return reflogs.observe_refresh_reflogs(root.parent, (observed,), request, owner)

    def transitions(self, records, identity=IDENT):
        return reflogs.refresh_reflog_transitions(
            SimpleNamespace(reflogs=records), identity
        )

    def assert_native(self, root=None):
        root = root or self.root
        target = self.target(root)
        before = snapshot(root)
        records = self.observe(root, target)
        transitions = self.transitions(records)
        self.assertEqual(snapshot(root), before)
        with patch.dict(
            os.environ,
            {
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.test",
                "GIT_COMMITTER_DATE": "1700000000 +0000",
            },
        ):
            git(
                root,
                "update-ref",
                "-m",
                "literate-ai refresh",
                "HEAD",
                target,
                records[0].previous_commit,
            )
        for item in transitions:
            actual = (
                item.before.path.read_bytes() if item.before.path.exists() else None
            )
            self.assertEqual(actual, item.prospective, item.name)
        return records

    def test_attached_chain_matches_native_git_for_every_hop(self):
        git(self.root, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
        git(self.root, "symbolic-ref", "HEAD", "refs/heads/alias")
        records = self.assert_native()
        self.assertEqual(
            [item.name for item in records],
            ["HEAD", "refs/heads/alias", "refs/heads/main"],
        )

    def test_detached_head_matches_native_git(self):
        git(self.root, "checkout", "--detach", "-q", "HEAD")
        self.assertEqual([item.name for item in self.assert_native()], ["HEAD"])

    def test_existing_logs_append_even_when_auto_creation_is_disabled(self):
        git(self.root, "config", "core.logAllRefUpdates", "false")
        self.assertTrue(all(item.append for item in self.assert_native()))

    def test_missing_enabled_logs_and_unset_policy_match_native_git(self):
        git(self.root, "config", "--unset", "core.logAllRefUpdates")
        for name in ("HEAD", "refs/heads/main"):
            (self.root / ".git/logs" / name).unlink()
        records = self.assert_native()
        self.assertTrue(all(item.append for item in records))
        self.assertTrue(all(item.before.content is None for item in records))

    def test_missing_disabled_logs_remain_absent_without_identity(self):
        git(self.root, "config", "core.logAllRefUpdates", "false")
        for name in ("HEAD", "refs/heads/main"):
            (self.root / ".git/logs" / name).unlink()
        records = self.assert_native()
        self.assertTrue(all(not item.append for item in records))
        self.assertTrue(
            all(item.prospective is None for item in self.transitions(records, None))
        )

    def test_true_and_always_follow_native_custom_reference_policy(self):
        for policy in ("true", "always"):
            with self.subTest(policy=policy):
                git(self.root, "config", "core.logAllRefUpdates", policy)
                git(self.root, "update-ref", "refs/custom/topic", "HEAD")
                git(self.root, "symbolic-ref", "HEAD", "refs/custom/topic")
                records = self.assert_native()
                custom = next(
                    item for item in records if item.name == "refs/custom/topic"
                )
                self.assertEqual(custom.append, policy == "always")

    def test_linked_worktree_private_and_shared_log_paths_match_native_git(self):
        linked = self.root / "linked"
        git(self.root, "worktree", "add", "--detach", str(linked), "HEAD")
        git(linked, "config", "core.logAllRefUpdates", "always")
        git(linked, "update-ref", "refs/worktree/topic", "HEAD")
        git(linked, "symbolic-ref", "HEAD", "refs/worktree/topic")
        private = self.assert_native(linked)
        self.assertTrue(all(item.anchor != self.root / ".git" for item in private))
        git(linked, "checkout", "-b", "linked-topic")
        shared = self.assert_native(linked)
        self.assertNotEqual(shared[0].anchor, shared[1].anchor)
        self.assertEqual(shared[1].anchor, self.root / ".git")

    def test_bare_backed_linked_worktree_default_matches_native_git(self):
        bare = self.root / "bare.git"
        git(self.root, "clone", "--bare", str(self.root), str(bare))
        git(bare, "config", "user.name", "Test")
        git(bare, "config", "user.email", "test@example.test")
        linked = self.root / "bare-linked"
        git(bare, "worktree", "add", str(linked), "main")
        self.assert_native(linked)

    def test_sha256_logs_match_native_git(self):
        other = self.root / "sha256"
        try:
            repository(other, sha256=True)
        except subprocess.CalledProcessError:
            self.skipTest("Git lacks SHA-256 support")
        records = self.assert_native(other)
        self.assertTrue(all(len(item.previous_commit) == 64 for item in records))

    def test_exact_noop_does_not_observe_or_append_logs(self):
        current = git(self.root, "rev-parse", "HEAD").decode().strip()
        with patch.object(reflogs, "_policy", side_effect=AssertionError("no-op")):
            self.assertEqual(self.observe(target=current), ())

    def test_same_byte_log_replacement_changes_custody(self):
        target = self.target(self.root)
        before = self.observe(target=target)
        path = before[0].before.path
        replacement = path.with_name("replacement")
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        after = self.observe(target=target)
        self.assertNotEqual(before, after)
        self.assertEqual(before[0].before.content, after[0].before.content)

    def test_foreign_log_lock_is_preserved(self):
        marker = self.root / ".git/logs/HEAD.lock"
        marker.write_bytes(b"foreign writer")
        target = self.target(self.root)
        before = snapshot(self.root)
        with self.assertRaises(OrchestrationInventoryError):
            self.observe(target=target)
        self.assertEqual(snapshot(self.root), before)

    def test_redirected_log_path_is_refused(self):
        with patch.object(reflogs, "_git_path", return_value=self.root / "foreign"):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                self.observe()
        self.assertEqual(caught.exception.code, "orchestration.refresh_reflog_path")
        self.assertFalse((self.root / "foreign").exists())

    def test_truncated_nul_and_hardlinked_logs_refuse_without_repair(self):
        path = self.root / ".git/logs/HEAD"
        for content in (b"truncated", b"bad\0record\n"):
            path.write_bytes(content)
            with self.assertRaises(OrchestrationInventoryError):
                self.observe()
            self.assertEqual(path.read_bytes(), content)
        path.write_bytes(b"retained historical bytes\n")
        os.link(path, self.root / ".git/retained-log")
        with self.assertRaises(OrchestrationInventoryError):
            self.observe()
        self.assertEqual(path.stat().st_nlink, 2)

    def test_capture_and_prospective_bounds_refuse(self):
        records = self.observe()
        for limit in ("_MAX_LOG_BYTES", "_MAX_TOTAL_BYTES", "_MAX_LOGS"):
            with patch.object(reflogs, limit, 1):
                with self.assertRaises(OrchestrationInventoryError):
                    self.observe()
        for limit in ("_MAX_LOG_BYTES", "_MAX_TOTAL_BYTES"):
            with patch.object(reflogs, limit, len(records[0].before.content)):
                with self.assertRaises(OrchestrationInventoryError):
                    self.transitions(records)

    def test_shared_log_deduplication_and_conflict_refusal(self):
        record = self.observe()[0]
        shared = replace(record, repository="other")
        self.assertEqual(len(self.transitions((record, shared))), 1)
        for changed in (
            replace(shared, prospective_commit="f" * 40),
            replace(shared, append=False),
        ):
            with self.assertRaises(OrchestrationInventoryError):
                self.transitions((record, changed))
        disabled = replace(
            record,
            append=False,
            before=replace(record.before, content=None, signature=None),
        )
        self.assertEqual(len(self.transitions((disabled, disabled), None)), 1)

    def test_committer_control_characters_and_malformed_values_refuse(self):
        records = self.observe()
        for identity in (
            None,
            IDENT.decode(),
            b"",
            IDENT + b"\n",
            IDENT.replace(b"Test", b"Te\0st"),
            IDENT.replace(b"+0000", b"+9999"),
        ):
            with (
                self.subTest(identity=identity),
                self.assertRaises(OrchestrationInventoryError),
            ):
                self.transitions(records, identity)

    def test_staging_reads_one_root_committer_and_rechecks_custody(self):
        records = self.observe()
        refresh = SimpleNamespace(
            reflogs=records, repository=SimpleNamespace(root=self.root)
        )
        files = SimpleNamespace(
            require_current=Mock(),
            target_modes=(),
            _owner=SimpleNamespace(_prepared=SimpleNamespace(refresh=refresh)),
        )
        lock, manifest = object(), object()
        with (
            patch.object(
                metadata, "refresh_metadata_transitions", return_value=(lock, manifest)
            ),
            patch.object(reflogs, "_git", return_value=IDENT + b"\n") as command,
        ):
            result = reflogs.prepare_staged_metadata(files)
        command.assert_called_once_with(self.root, "var", "GIT_COMMITTER_IDENT")
        self.assertEqual(files.require_current.call_count, 2)
        self.assertEqual(result[-2:], (lock, manifest))
        files.require_current.reset_mock()
        with (
            patch.object(
                metadata, "refresh_metadata_transitions", return_value=(lock, manifest)
            ),
            patch.object(reflogs, "_git", return_value=b"invalid"),
            self.assertRaises(OrchestrationInventoryError),
        ):
            reflogs.prepare_staged_metadata(files)
        self.assertEqual(files.require_current.call_count, 2)
        refresh.reflogs = ()
        with (
            patch.object(
                metadata, "refresh_metadata_transitions", return_value=(lock, manifest)
            ),
            patch.object(reflogs, "_git", side_effect=AssertionError("no logs")),
        ):
            self.assertEqual(reflogs.prepare_staged_metadata(files), (lock, manifest))
