from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_repository_refresh_refs``."""

import os

import subprocess

import tempfile

import unittest

from pathlib import Path

from unittest.mock import patch

from literate_ai.adapters import repository_refresh as refresh

from literate_ai.adapters import repository_refresh_refs as refs

from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError

from tests.support.fixtures_test_repository_orchestration import git, snapshot

class RefreshReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.test")
        (self.root / "source").write_bytes(b"original\n")
        git(self.root, "add", "source")
        git(self.root, "commit", "-q", "-m", "initial")

    def observe(self, root=None):
        return refresh._observe_git(root or self.root, clean=True)

    def test_loose_and_detached_observations_preserve_all_file_bytes_and_mtimes(self):
        before = snapshot(self.root)
        attached = self.observe()
        self.assertEqual(
            [item.name for item in attached.references], ["refs/heads/main"]
        )
        self.assertEqual(
            attached.references[0].file.content, (attached.commit + "\n").encode()
        )
        self.assertIsNone(attached.packed_references.content)
        self.assertEqual(self.observe(), attached)
        self.assertEqual(snapshot(self.root), before)
        git(self.root, "checkout", "--detach", "-q", "HEAD")
        before = snapshot(self.root)
        detached = self.observe()
        self.assertEqual(detached.references, ())
        self.assertIsNone(detached.symbolic_reference)
        self.assertEqual(snapshot(self.root), before)

    def test_same_commit_and_terminal_name_cannot_hide_intermediate_chain_changes(self):
        git(self.root, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
        git(self.root, "symbolic-ref", "refs/heads/middle", "refs/heads/main")
        git(self.root, "symbolic-ref", "HEAD", "refs/heads/alias")
        before = self.observe()
        git(self.root, "symbolic-ref", "refs/heads/alias", "refs/heads/middle")
        after = self.observe()
        self.assertEqual(before.head, after.head)
        self.assertEqual(before.commit, after.commit)
        self.assertEqual(before.symbolic_reference, after.symbolic_reference)
        self.assertNotEqual(before.references, after.references)

    def test_loose_to_packed_and_same_bytes_inode_replacement_change_custody(self):
        before = self.observe()
        path = before.references[0].file.path
        replacement = path.with_name("replacement")
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        after = self.observe()
        self.assertEqual(before.commit, after.commit)
        self.assertNotEqual(before.references, after.references)
        git(self.root, "pack-refs", "--all", "--prune")
        packed = self.observe()
        self.assertEqual(packed.commit, before.commit)
        self.assertIsNone(packed.references[0].file.content)
        self.assertIsNotNone(packed.packed_references.content)

    def test_packed_nested_branch_can_have_no_loose_parent(self):
        git(self.root, "checkout", "-q", "-b", "nested/topic")
        git(self.root, "pack-refs", "--all", "--prune")
        parent = self.root / ".git/refs/heads/nested"
        if parent.exists():
            parent.rmdir()
        before = snapshot(self.root)
        observed = self.observe()
        self.assertIsNone(observed.references[0].file.content)
        self.assertFalse(parent.exists())
        self.assertEqual(snapshot(self.root), before)

    def test_linked_worktree_private_ref_uses_its_own_git_directory(self):
        worktree = self.root / "linked"
        git(self.root, "worktree", "add", "--detach", str(worktree), "HEAD")
        commit = git(worktree, "rev-parse", "HEAD").decode().strip()
        git(worktree, "update-ref", "refs/worktree/topic", commit)
        git(worktree, "symbolic-ref", "HEAD", "refs/worktree/topic")
        before = snapshot(self.root)
        observed = self.observe(worktree)
        self.assertNotEqual(observed.git_directory, observed.common_directory)
        self.assertEqual(
            observed.references[0].file.path,
            observed.git_directory / "refs/worktree/topic",
        )
        self.assertEqual(snapshot(self.root), before)

    def test_reference_and_packed_writer_markers_refuse_without_deletion(self):
        for relative in ("refs/heads/main.lock", "packed-refs.lock"):
            marker = self.root / ".git" / relative
            marker.write_bytes(b"foreign writer")
            before = snapshot(self.root)
            with (
                self.subTest(relative=relative),
                self.assertRaises(OrchestrationInventoryError),
            ):
                self.observe()
            self.assertEqual(snapshot(self.root), before)
            marker.unlink()

    def test_chain_and_packed_byte_bounds_refuse(self):
        git(self.root, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
        git(self.root, "symbolic-ref", "HEAD", "refs/heads/alias")
        with (
            patch.object(refs, "_MAX_CHAIN", 1),
            self.assertRaises(OrchestrationInventoryError),
        ):
            self.observe()
        git(self.root, "pack-refs", "--all", "--prune")
        with (
            patch.object(refs, "_MAX_PACKED_BYTES", 1),
            self.assertRaises(OrchestrationInventoryError),
        ):
            self.observe()

    def test_hardlinked_reference_is_not_admitted_as_a_future_write_target(self):
        path = self.root / ".git/refs/heads/main"
        os.link(path, self.root / ".git/retained-ref")
        before = snapshot(self.root)
        with self.assertRaises(OrchestrationInventoryError):
            self.observe()
        self.assertEqual(snapshot(self.root), before)

    def test_other_reference_storage_backend_is_explicitly_unsupported(self):
        with patch.object(
            refresh, "_git", return_value=b"extensions.refstorage\nreftable\0"
        ):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                refresh._configuration(self.root, clean=True)
        self.assertEqual(
            caught.exception.code, "orchestration.refresh_refs_unsupported"
        )

    def test_sha256_reference_bytes_remain_exact(self):
        other = self.root / "sha256"
        other.mkdir()
        try:
            git(other, "init", "-q", "-b", "main", "--object-format=sha256")
        except subprocess.CalledProcessError:
            self.skipTest("Git lacks SHA-256 support")
        git(other, "config", "user.name", "Test")
        git(other, "config", "user.email", "test@example.test")
        (other / "source").write_bytes(b"source\n")
        git(other, "add", "source")
        git(other, "commit", "-q", "-m", "sha256")
        git(other, "pack-refs", "--all", "--prune")
        before = snapshot(other)
        observed = self.observe(other)
        self.assertEqual(len(observed.commit), 64)
        self.assertIsNone(observed.references[0].file.content)
        self.assertEqual(snapshot(other), before)

