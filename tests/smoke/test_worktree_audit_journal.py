"""Observation history is resumable, private and never retirement authority."""

from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import project_worktree_audit_inputs as inputs
from literate_ai.adapters import project_worktree_audit_journal as journal
from literate_ai.adapters.project_worktrees import ProjectWorktreeError
from literate_ai.contracts.identity import ContentIdentity
from tests.support.symlinks import skip_unless_symlinks_followable
from tests.support.worktree_repositories import WorktreeRepositoryFixture


class WorktreeAuditJournalTests(WorktreeRepositoryFixture, unittest.TestCase):
    def prepare(self):
        return inputs.capture_worktree_audit_inputs(self.root, self.root)

    def scope(self, prepared):
        document = json.loads(prepared.canonical_bytes)
        identity = ContentIdentity.from_dict(
            document["common_directory_observation_identity"]
        )
        root = self.parent / "evidence"
        return root, root / identity.digest, identity

    def test_idempotent_history_does_not_reconstruct_a_live_owner(self):
        prepared = self.prepare()
        root, scope, identity = self.scope(prepared)
        first = journal.record_worktree_audit_inputs(prepared, root)
        second = journal.record_worktree_audit_inputs(prepared, root)
        self.assertEqual(first, second)
        self.assertEqual(first.history_length, 1)
        loaded = journal.load_worktree_audit_input_observation(
            root, identity, self.root
        )
        self.assertEqual(loaded, first)
        self.assertFalse(loaded.to_dict()["audit_complete"])
        self.assertFalse(loaded.to_dict()["retirement_authorized"])
        with self.assertRaises(ProjectWorktreeError):
            journal.record_worktree_audit_inputs(loaded.to_dict(), root)
        if os.name != "nt":
            for path in scope.rglob("*.json"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_changed_inputs_append_history_and_preserve_original_observation(self):
        prepared = self.prepare()
        root, scope, identity = self.scope(prepared)
        original = journal.record_worktree_audit_inputs(prepared, root)
        original_path = (
            scope / "observations" / (original.observation_identity.digest + ".json")
        )
        original_bytes = original_path.read_bytes()
        (self.root / "source").write_bytes(b"new source bytes")
        newer = journal.record_worktree_audit_inputs(self.prepare(), root)
        self.assertNotEqual(original.event_identity, newer.event_identity)
        self.assertEqual(newer.history_length, 2)
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(
            journal.load_worktree_audit_input_observation(root, identity, self.root),
            newer,
        )

    def test_interruption_before_head_publication_resumes_without_overwrite(self):
        prepared = self.prepare()
        root, scope, _ = self.scope(prepared)
        first = journal.record_worktree_audit_inputs(prepared, root)
        head = next((scope / "heads").glob("*.json"))
        before = head.read_bytes()
        (self.root / "source").write_bytes(b"next observation")
        updated = self.prepare()
        publish = journal._publish

        def interrupted(path, *args, **kwargs):
            if path.parent.name == "heads":
                raise OSError("injected publication interruption")
            return publish(path, *args, **kwargs)

        with mock.patch.object(journal, "_publish", side_effect=interrupted):
            with self.assertRaises(ProjectWorktreeError):
                journal.record_worktree_audit_inputs(updated, root)
        self.assertEqual(head.read_bytes(), before)
        resumed = journal.record_worktree_audit_inputs(updated, root)
        self.assertEqual(resumed.history_length, 2)
        self.assertNotEqual(resumed.event_identity, first.event_identity)
        self.assertEqual(len(tuple((scope / "events").glob("*.json"))), 2)

    def test_corrupt_published_evidence_is_refused_without_repair_or_eviction(self):
        prepared = self.prepare()
        root, scope, identity = self.scope(prepared)
        recorded = journal.record_worktree_audit_inputs(prepared, root)
        head = next((scope / "heads").glob("*.json"))
        before = head.read_bytes()
        observation = (
            scope / "observations" / (recorded.observation_identity.digest + ".json")
        )
        observation.write_bytes(b"{}")
        with self.assertRaises(ProjectWorktreeError):
            journal.record_worktree_audit_inputs(prepared, root)
        with self.assertRaises(ProjectWorktreeError):
            journal.load_worktree_audit_input_observation(root, identity, self.root)
        self.assertEqual(head.read_bytes(), before)
        self.assertEqual(observation.read_bytes(), b"{}")

    def test_byte_and_event_limits_refuse_before_replacing_current_evidence(self):
        prepared = self.prepare()
        root, scope, _ = self.scope(prepared)
        with mock.patch.object(journal, "_MAX_STORE_BYTES", 1):
            with self.assertRaises(ProjectWorktreeError):
                journal.record_worktree_audit_inputs(prepared, root)
        self.assertFalse(tuple(scope.rglob("*.json")))
        journal.record_worktree_audit_inputs(prepared, root)
        head = next((scope / "heads").glob("*.json"))
        before = head.read_bytes()
        (self.root / "source").write_bytes(b"too many observations")
        with mock.patch.object(journal, "_MAX_EVENTS", 1):
            with self.assertRaises(ProjectWorktreeError):
                journal.record_worktree_audit_inputs(self.prepare(), root)
        self.assertEqual(head.read_bytes(), before)

    def test_stale_owner_and_overlapping_source_roots_create_no_journal(self):
        prepared = self.prepare()
        root, _, _ = self.scope(prepared)
        for unsafe in (
            self.root / "evidence",
            self.root / ".git/evidence",
            self.parent,
        ):
            with (
                self.subTest(path=unsafe.name),
                self.assertRaises(ProjectWorktreeError),
            ):
                journal.record_worktree_audit_inputs(prepared, unsafe)
        (self.root / "source").write_bytes(b"changed")
        with self.assertRaises(ProjectWorktreeError):
            journal.record_worktree_audit_inputs(prepared, root)
        self.assertFalse(root.exists())

    def test_linked_capture_protects_canonical_and_other_registered_sources(self):
        linked = self.linked(self.parent / "task")
        other = self.linked(self.parent / "other")
        prepared = inputs.capture_worktree_audit_inputs(self.root, linked)
        for checkout in (self.root, linked, other):
            destination = checkout / "journal-evidence"
            with self.subTest(checkout=checkout.name):
                with self.assertRaises(ProjectWorktreeError):
                    journal.record_worktree_audit_inputs(prepared, destination)
                self.assertFalse(destination.exists())

    def test_reading_an_absent_store_does_not_create_it(self):
        prepared = self.prepare()
        root, _, identity = self.scope(prepared)
        self.assertIsNone(
            journal.load_worktree_audit_input_observation(root, identity, self.root)
        )
        self.assertFalse(root.exists())

    def test_link_like_journal_root_is_not_followed(self):
        skip_unless_symlinks_followable(self, directory=self.parent)
        prepared = self.prepare()
        root, _, _ = self.scope(prepared)
        root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ProjectWorktreeError):
            journal.record_worktree_audit_inputs(prepared, root)
        self.assertFalse(any(p.name == "observations" for p in self.root.iterdir()))

    def test_orphaned_temporary_link_is_retained_and_does_not_prevent_resume(self):
        prepared = self.prepare()
        root, scope, _ = self.scope(prepared)
        unlink = Path.unlink

        def interrupted(path, *args, **kwargs):
            if path.name.startswith(".pending-"):
                raise OSError("injected interruption after immutable link")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", interrupted):
            with self.assertRaises(ProjectWorktreeError):
                journal.record_worktree_audit_inputs(prepared, root)
        retained = tuple(scope.rglob(".pending-*.json"))
        self.assertTrue(retained)
        recorded = journal.record_worktree_audit_inputs(prepared, root)
        self.assertEqual(recorded.history_length, 1)
        self.assertTrue(all(path.exists() for path in retained))


if __name__ == "__main__":
    unittest.main()
