"""Real Git operation payloads remain live custody inputs, never retirement proof."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import project_worktree_operation_inputs as operations
from literate_ai.adapters.project_worktree_audit_inputs import (
    WorktreeAuditLimits,
    capture_worktree_audit_inputs,
)
from literate_ai.adapters.project_worktrees import ProjectWorktreeError
from tests.support.symlinks import skip_unless_symlinks_followable
from tests.support.worktree_repositories import WorktreeRepositoryFixture


class WorktreeOperationInputTests(WorktreeRepositoryFixture, unittest.TestCase):
    def git_path(self, checkout, name):
        return Path(
            self.git(
                checkout, "rev-parse", "--path-format=absolute", "--git-path", name
            )
            .decode()
            .strip()
        )

    def paused_rebase(self, checkout):
        initial = self.git(checkout, "rev-parse", "HEAD").strip().decode()
        self.git(checkout, "checkout", "-qb", "audit-feature")
        (checkout / "source").write_bytes(b"feature change\n")
        self.git(checkout, "add", "source")
        self.git(checkout, "commit", "-qm", "feature")
        self.git(checkout, "checkout", "--detach", initial)
        (checkout / "source").write_bytes(b"competing change\n")
        self.git(checkout, "add", "source")
        self.git(checkout, "commit", "-qm", "competing")
        upstream = self.git(checkout, "rev-parse", "HEAD").strip().decode()
        self.git(checkout, "checkout", "audit-feature")
        with self.assertRaises(subprocess.CalledProcessError) as stopped:
            self.git(checkout, "-c", "rebase.backend=merge", "rebase", upstream)
        self.assertEqual(stopped.exception.returncode, 1)
        directory = self.git_path(checkout, "rebase-merge")
        self.assertTrue((directory / "git-rebase-todo").is_file())
        return directory

    def assert_rebase_custody(self, checkout):
        directory = self.paused_rebase(checkout)
        original = {
            p.relative_to(directory).as_posix(): p.read_bytes()
            for p in directory.rglob("*")
            if p.is_file()
        }
        prepared = capture_worktree_audit_inputs(self.root, checkout)
        document = json.loads(prepared.canonical_bytes)
        inputs = {
            entry["path"]: entry
            for entry in document["git_observations"]["operation_inputs"]
            if entry["anchor"] == str(directory.parent)
        }
        for name, content in original.items():
            entry = inputs["rebase-merge/" + name]
            self.assertEqual(base64.b64decode(entry["wire_base64"]), content)
            self.assertEqual(
                entry["content_identity"],
                "sha256:" + hashlib.sha256(content).hexdigest(),
            )
        self.assertIn("git-operation-state", document["pending_checks"])
        self.assertFalse(document["audit_complete"])
        self.assertFalse(document["retirement_authorized"])
        prepared.require_current()
        self.assertEqual(
            original,
            {
                p.relative_to(directory).as_posix(): p.read_bytes()
                for p in directory.rglob("*")
                if p.is_file()
            },
        )
        todo = directory / "git-rebase-todo"
        before = todo.stat()
        todo.write_bytes(todo.read_bytes() + b"# changed operator todo\n")
        os.utime(todo, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(ProjectWorktreeError):
            prepared.require_current()

    def test_real_canonical_rebase_payloads_are_retained_and_drift_refuses(self):
        self.assert_rebase_custody(self.root)

    def test_real_linked_rebase_payloads_use_the_owned_administration_directory(self):
        self.assert_rebase_custody(self.linked(self.parent / "task"))

    def test_new_operation_descendant_invalidates_live_capture(self):
        directory = self.paused_rebase(self.root)
        prepared = capture_worktree_audit_inputs(self.root, self.root)
        (directory / "retained-extra").write_bytes(b"new operator data")
        with self.assertRaises(ProjectWorktreeError):
            prepared.require_current()
        self.assertEqual(
            (directory / "retained-extra").read_bytes(), b"new operator data"
        )

    def test_mid_capture_directory_addition_refuses_without_removing_data(self):
        directory = self.paused_rebase(self.root)
        original_reader = operations._read_document
        added = directory / "concurrent-extra"

        def changed(path, **kwargs):
            result = original_reader(path, **kwargs)
            if path == directory / "git-rebase-todo" and not added.exists():
                added.write_bytes(b"concurrent operator data")
            return result

        with mock.patch.object(operations, "_read_document", side_effect=changed):
            with self.assertRaises(ProjectWorktreeError):
                capture_worktree_audit_inputs(self.root, self.root)
        self.assertEqual(added.read_bytes(), b"concurrent operator data")

    def operation_directory(self):
        directory = self.git_path(self.root, "rebase-merge")
        directory.mkdir()
        return directory

    def test_operation_payload_file_bound_is_enforced_before_read(self):
        directory = self.operation_directory()
        (directory / "oversized").write_bytes(b"x" * 64)
        limits = replace(WorktreeAuditLimits(), maximum_file_bytes=32)
        with self.assertRaisesRegex(ProjectWorktreeError, "per-file"):
            capture_worktree_audit_inputs(self.root, self.root, limits=limits)

    def test_operation_membership_obeys_entry_bound(self):
        directory = self.operation_directory()
        for number in range(5):
            (directory / str(number)).write_bytes(b"")
        limits = replace(WorktreeAuditLimits(), maximum_entries=4)
        with self.assertRaisesRegex(ProjectWorktreeError, "entry limit"):
            capture_worktree_audit_inputs(self.root, self.root, limits=limits)

    def test_operation_wire_obeys_aggregate_metadata_bound(self):
        directory = self.operation_directory()
        (directory / "payload").write_bytes(b"x" * 1500)
        limits = replace(WorktreeAuditLimits(), maximum_metadata_bytes=2048)
        with self.assertRaisesRegex(ProjectWorktreeError, "metadata"):
            capture_worktree_audit_inputs(self.root, self.root, limits=limits)

    def test_operation_alias_refuses_without_reading_its_external_target(self):
        skip_unless_symlinks_followable(self, directory=self.parent)
        directory = self.operation_directory()
        outside = self.parent / "outside"
        outside.write_bytes(b"outside bytes")
        (directory / "alias").symlink_to(outside)
        with self.assertRaises(ProjectWorktreeError):
            capture_worktree_audit_inputs(self.root, self.root)
        self.assertEqual(outside.read_bytes(), b"outside bytes")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "native POSIX special files")
    def test_operation_fifo_refuses_without_opening_a_blocking_reader(self):
        directory = self.operation_directory()
        os.mkfifo(directory / "pipe")
        with self.assertRaises(ProjectWorktreeError):
            capture_worktree_audit_inputs(self.root, self.root)


if __name__ == "__main__":
    unittest.main()
