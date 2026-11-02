"""Real read-only Git/source captures do not imply a complete retirement audit."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import sys
import unittest
from dataclasses import replace
from unittest import mock

from literate_ai.adapters import project_worktree_audit_inputs as audit
from literate_ai.adapters.project_worktrees import ProjectWorktreeError
from tests.support.symlinks import skip_unless_symlinks_followable
from tests.support.worktree_repositories import WorktreeRepositoryFixture


class WorktreeAuditInputTests(WorktreeRepositoryFixture, unittest.TestCase):
    def capture(self, checkout=None, **kwargs):
        prepared = audit.capture_worktree_audit_inputs(
            self.root, checkout or self.root, **kwargs
        )
        return prepared, json.loads(prepared.canonical_bytes)

    def test_complete_source_facts_do_not_assert_activity_or_retirement(self):
        checkout = self.linked(self.parent / "task")
        prepared, document = self.capture(checkout)
        files = {entry["path"]: entry for entry in document["source"]}
        self.assertEqual(
            files["source"]["content_identity"],
            "sha256:" + hashlib.sha256(b"original source\n").hexdigest(),
        )
        self.assertIn(".git", files)
        self.assertFalse(document["audit_complete"])
        self.assertFalse(document["retirement_authorized"])
        self.assertIn("process-activity", document["pending_checks"])
        prepared.require_current()
        with self.assertRaises(AttributeError):
            prepared.checkout = self.parent
        self.assertEqual(prepared.identity, self.capture(checkout)[0].identity)

    def test_staged_working_untracked_and_ignored_inputs_all_remain(self):
        checkout = self.linked(self.parent / "task")
        (checkout / "source").write_bytes(b"staged source\n")
        self.git(checkout, "add", "source")
        (checkout / "source").write_bytes(b"different working source\n")
        (checkout / ".gitignore").write_text("ignored\n")
        (checkout / "ignored").write_bytes(b"retained ignored evidence")
        (checkout / "untracked").write_bytes(b"operator data")
        before = {p.name: p.read_bytes() for p in checkout.iterdir() if p.is_file()}
        prepared, document = self.capture(checkout)
        files = {entry["path"]: entry for entry in document["source"]}
        self.assertTrue(
            {"source", "ignored", "untracked", ".gitignore"}.issubset(files)
        )
        self.assertGreater(document["git_observations"]["staged_changes"]["bytes"], 0)
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in checkout.iterdir() if p.is_file()}
        )
        prepared.require_current()

    def test_raw_git_observation_does_not_execute_filters_diff_or_fsmonitor(self):
        checkout = self.linked(self.parent / "task")
        marker = self.parent / "executed"
        script = self.parent / "filter.py"
        script.write_text(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')\n"
        )
        command = shlex.join((sys.executable, str(script)))
        for key in (
            "filter.unsafe.clean",
            "filter.unsafe.process",
            "diff.external",
            "core.fsmonitor",
        ):
            self.git(checkout, "config", key, command)
        (checkout / ".gitattributes").write_text("source filter=unsafe\n")
        self.capture(checkout)
        self.assertFalse(marker.exists())

    def test_symlinks_are_captured_without_reading_outside_bytes(self):
        skip_unless_symlinks_followable(self, directory=self.parent)
        outside = self.parent / "outside-secret"
        outside.write_bytes(b"outside bytes must not enter source capture")
        (self.root / "alias").symlink_to(outside)
        _, document = self.capture()
        alias = next(entry for entry in document["source"] if entry["path"] == "alias")
        self.assertEqual(alias["kind"], "symlink")
        self.assertEqual(
            base64.b64decode(alias["target_base64"]),
            os.fsencode(os.readlink(self.root / "alias")),
        )
        self.assertNotIn("content_identity", alias)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "native POSIX special files")
    def test_special_file_is_pending_and_never_opened_as_source(self):
        os.mkfifo(self.root / "pipe")
        _, document = self.capture()
        self.assertIn(
            {"code": "special-file-audit-required", "path": "pipe"},
            document["pending_findings"],
        )
        self.assertFalse(document["audit_complete"])

    def test_nested_git_directory_requires_its_own_audit(self):
        self.initialize(self.root / "nested")
        _, document = self.capture()
        self.assertIn(
            {"code": "nested-repository-audit-required", "path": "nested/.git"},
            document["pending_findings"],
        )
        self.assertFalse(
            any(
                entry["path"].startswith("nested/.git/") for entry in document["source"]
            )
        )
        self.assertFalse(document["audit_complete"])

    def test_reference_reflog_and_unreachable_object_facts_are_retained(self):
        checkout = self.linked(self.parent / "task")
        self.git(checkout, "commit", "--allow-empty", "-qm", "temporary")
        orphan = self.git(checkout, "rev-parse", "HEAD").strip()
        self.git(checkout, "reset", "--hard", "HEAD~")
        (checkout / "source").write_bytes(b"stash content")
        self.git(checkout, "stash", "push", "-qm", "fixture")
        _, document = self.capture(checkout)
        observations = document["git_observations"]
        self.assertIn(
            b"refs/stash", base64.b64decode(observations["references"]["wire_base64"])
        )
        self.assertIn(
            orphan, base64.b64decode(observations["reflog_roots"]["wire_base64"])
        )
        self.assertIn(
            orphan, base64.b64decode(observations["object_inventory"]["wire_base64"])
        )

    def test_observation_does_not_refresh_another_worktrees_split_index(self):
        selected = self.linked(self.parent / "selected")
        peer = self.linked(self.parent / "split-peer")
        self.git(peer, "update-index", "--split-index")
        shared = self.git(peer, "rev-parse", "--shared-index-path").decode().strip()
        from pathlib import Path

        backing = Path(shared)
        if not backing.is_absolute():
            backing = peer / backing
        os.utime(backing, ns=(1_000_000_000, 1_000_000_000))
        before = backing.stat().st_mtime_ns
        self.capture(selected)
        self.assertEqual(backing.stat().st_mtime_ns, before)

    def test_changed_source_refuses_a_mixed_snapshot(self):
        capture = audit._source_snapshot
        calls = 0

        def changing(*args):
            nonlocal calls
            result = capture(*args)
            calls += 1
            if calls == 1:
                (self.root / "source").write_bytes(b"concurrent source")
            return result

        with mock.patch.object(audit, "_source_snapshot", side_effect=changing):
            with self.assertRaisesRegex(ProjectWorktreeError, "changed"):
                self.capture()

    def test_replay_is_not_a_live_capture_owner(self):
        prepared, _ = self.capture()
        with self.assertRaises(TypeError):
            audit.PreparedWorktreeAuditInputs(
                None, self.root, self.root, prepared.limits, {}
            )
        (self.root / "source").write_bytes(b"changed after capture")
        with self.assertRaisesRegex(ProjectWorktreeError, "changed"):
            prepared.require_current()

    def test_entry_file_total_and_metadata_limits_fail_closed(self):
        limits = audit.WorktreeAuditLimits()
        with self.assertRaises(ProjectWorktreeError):
            self.capture(limits=replace(limits, maximum_entries=1))
        with self.assertRaisesRegex(ProjectWorktreeError, "per-file"):
            self.capture(limits=replace(limits, maximum_file_bytes=1))
        (self.root / "one").write_bytes(b"a" * 30)
        (self.root / "two").write_bytes(b"b" * 30)
        with self.assertRaisesRegex(ProjectWorktreeError, "source byte"):
            self.capture(
                limits=replace(limits, maximum_file_bytes=64, maximum_total_bytes=64)
            )
        with self.assertRaisesRegex(ProjectWorktreeError, "metadata"):
            self.capture(limits=replace(limits, maximum_metadata_bytes=512))
        for name, value in (
            ("maximum_entries", True),
            ("timeout_seconds", 0),
            ("maximum_file_bytes", -1),
        ):
            with self.subTest(name=name), self.assertRaises(ProjectWorktreeError):
                replace(limits, **{name: value})

    def test_unregistered_and_missing_checkouts_cannot_supply_audit_inputs(self):
        foreign = self.parent / "independent"
        self.initialize(foreign)
        for path in (foreign, self.parent / "missing"):
            with self.subTest(path=path.name), self.assertRaises(ProjectWorktreeError):
                self.capture(path)


if __name__ == "__main__":
    unittest.main()
