"""Root-only lock writes, no-write inspection and concurrent-change preservation."""

from __future__ import annotations

import json
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_locks as storage
from literate_ai.adapters.repository_lock_planning import prepare_repository_lock
from literate_ai.adapters.repository_orchestration import (
    OrchestrationInventoryError,
    _read_document,
)
from literate_ai.contracts.identity import canonical_json_bytes
from tests.support import fixtures_test_repository_lock_planning as fixtures
from tests.support.fixtures_test_repository_orchestration import snapshot


class RepositoryLockStoreTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RepositoryLockPlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.base = fixture.root, fixture.base
        self.prepared = prepare_repository_lock(self.root)
        self.store = storage.RepositoryLockStore(self.root)

    def write_lock(self, lock):
        self.store.path.write_bytes(canonical_json_bytes(lock.to_dict()) + b"\n")

    def assert_refusal(self, operation, code):
        with self.assertRaises(OrchestrationInventoryError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "orchestration." + code)

    def test_missing_check_is_read_only_and_first_update_is_root_only(self):
        before = snapshot(self.base)
        self.assertEqual(self.store.check(self.prepared.lock)["state"], "missing")
        self.assertIsNone(self.store.read())
        self.assertEqual(snapshot(self.base), before)
        self.assertTrue(self.store.update(self.prepared))
        after = snapshot(self.base)
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(
            set(after) - set(before), {"super/" + storage.REPOSITORY_LOCK_PATH}
        )
        self.assertEqual(self.store.check(self.prepared.lock)["state"], "current")
        self.assertEqual(self.store.read(), self.prepared.lock)
        self.assertEqual(prepare_repository_lock(self.root), self.prepared)
        self.assertEqual(snapshot(self.base), after)

    def test_current_update_is_a_true_noop_without_writer_marker(self):
        self.store.update(self.prepared)
        before = snapshot(self.base)
        with patch.object(self.store, "_stage", side_effect=AssertionError("staging")):
            self.assertFalse(self.store.update(self.prepared))
        self.assertEqual(snapshot(self.base), before)

    def test_stale_canonical_lock_is_atomically_replaced(self):
        previous = replace(self.prepared.lock, project_id="old")
        self.write_lock(previous)
        check = self.store.check(self.prepared.lock)
        self.assertEqual(check["state"], "stale")
        self.assertEqual(check["current_identity"], previous.identity)
        original = storage.os.replace
        observed = []

        def replace_file(source, target):
            self.assertEqual(self.store.read(), previous)
            observed.append(Path(target))
            return original(source, target)

        with patch.object(storage.os, "replace", side_effect=replace_file):
            self.assertTrue(self.store.update(self.prepared))
        self.assertEqual(observed, [self.store.path])
        self.assertEqual(self.store.read(), self.prepared.lock)

    def test_malformed_and_noncanonical_locks_are_never_overwritten(self):
        canonical = canonical_json_bytes(self.prepared.lock.to_dict()) + b"\n"
        for content in (
            b"not JSON",
            canonical + b"\n",
            canonical.replace(b'"schema":', b'"schema":"duplicate","schema":', 1),
            canonical_json_bytes({**self.prepared.lock.to_dict(), "execution": True})
            + b"\n",
            canonical.replace(b"repository-lock@1", b"repository-lock@9"),
        ):
            with self.subTest(content=content[:30]):
                self.store.path.write_bytes(content)
                before = snapshot(self.base)
                for operation in (
                    self.store.read,
                    lambda: self.store.check(self.prepared.lock),
                    lambda: self.store.update(self.prepared),
                ):
                    self.assert_refusal(operation, "lock_invalid")
                self.assertEqual(snapshot(self.base), before)

    def test_directory_destination_refuses_without_writes(self):
        self.store.path.mkdir()
        before = snapshot(self.base)
        self.assert_refusal(lambda: self.store.update(self.prepared), "lock_invalid")
        self.assertEqual(snapshot(self.base), before)

    def test_link_destination_does_not_modify_its_target(self):
        target = self.base / "foreign.json"
        target.write_bytes(b"foreign")
        try:
            self.store.path.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        before = snapshot(self.base)
        self.assert_refusal(lambda: self.store.update(self.prepared), "lock_invalid")
        self.assertEqual(snapshot(self.base), before)

    def test_case_alias_refuses_on_every_platform(self):
        alias = self.store.directory / "Repository.Lock.json"
        alias.write_bytes(b"foreign")
        self.assert_refusal(self.store.read, "lock_path_unsafe")
        self.assertEqual(alias.read_bytes(), b"foreign")

    def test_oversized_lock_is_refused_before_opening(self):
        with self.store.path.open("wb") as stream:
            stream.seek(storage.MAXIMUM_LOCK_BYTES)
            stream.write(b"x")
        original_open = os.open

        def guarded(path, *args, **kwargs):
            if Path(path) == self.store.path:
                raise AssertionError("oversized read")
            return original_open(path, *args, **kwargs)

        with patch.object(storage.os, "open", side_effect=guarded):
            self.assert_refusal(self.store.read, "lock_invalid")
        self.assertEqual(self.store.path.stat().st_size, storage.MAXIMUM_LOCK_BYTES + 1)

    def test_oversized_candidate_refuses_before_any_write(self):
        before = snapshot(self.base)
        with patch.object(storage, "MAXIMUM_LOCK_BYTES", 1):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_limit_exceeded"
            )
        self.assertEqual(snapshot(self.base), before)

    def test_document_reader_has_explicit_positive_bounds(self):
        path = self.base / "bounded.json"
        path.write_bytes(b"12345")
        self.assertEqual(_read_document(path, maximum_bytes=5), b"12345")
        with self.assertRaises(OrchestrationInventoryError):
            _read_document(path, maximum_bytes=4)
        for limit in (0, -1, True, "5"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                _read_document(path, maximum_bytes=limit)

    def test_stale_preparation_does_not_create_a_writer_or_lock(self):
        manifest = self.root / "literate.project.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        before = snapshot(self.base)
        self.assert_refusal(lambda: self.store.update(self.prepared), "inputs_changed")
        self.assertEqual(snapshot(self.base), before)

    def test_prepared_root_mismatch_refuses_before_writes(self):
        other = replace(self.prepared, root=self.base)
        before = snapshot(self.base)
        self.assert_refusal(lambda: self.store.update(other), "lock_root_mismatch")
        self.assertEqual(snapshot(self.base), before)

    def test_existing_writer_marker_is_preserved_and_fails_fast(self):
        self.store.writer_path.write_bytes(b"other writer")
        before = snapshot(self.base)
        self.assert_refusal(lambda: self.store.update(self.prepared), "lock_busy")
        self.assertEqual(snapshot(self.base), before)

    def test_read_only_check_does_not_touch_a_writer_marker(self):
        self.write_lock(self.prepared.lock)
        self.store.writer_path.write_bytes(b"active writer")
        before = snapshot(self.base)
        self.assertEqual(self.store.check(self.prepared.lock)["state"], "current")
        self.assertEqual(snapshot(self.base), before)

    def test_another_store_cannot_write_while_publication_is_in_progress(self):
        other = storage.RepositoryLockStore(self.root)
        stage = self.store._stage

        def contend(*args):
            self.assert_refusal(lambda: other.update(self.prepared), "lock_busy")
            return stage(*args)

        with patch.object(self.store, "_stage", side_effect=contend):
            self.assertTrue(self.store.update(self.prepared))
        self.assertEqual(other.read(), self.prepared.lock)
        self.assertFalse(self.store.writer_path.exists())

    def test_partial_stage_write_cleans_only_owned_additions(self):
        before = snapshot(self.base)
        write = os.write
        count = 0

        def partial(descriptor, content):
            nonlocal count
            count += 1
            if count == 1:
                return write(descriptor, content[:7])
            raise OSError("simulated write failure")

        with patch.object(storage.os, "write", side_effect=partial):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_write_failed"
            )
        self.assertEqual(snapshot(self.base), before)

    def test_changed_stage_is_preserved_and_reported(self):
        stage = self.store._stage
        retained = []

        def change(*args):
            owned = stage(*args)
            owned.path.write_bytes(b"concurrent stage edit")
            retained.append(owned.path)
            return owned

        with patch.object(self.store, "_stage", side_effect=change):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_cleanup_incomplete"
            )
        self.assertEqual(retained[0].read_bytes(), b"concurrent stage edit")
        self.assertFalse(self.store.path.exists())
        self.assertFalse(self.store.writer_path.exists())

    def test_replaced_writer_marker_is_preserved(self):
        stage = self.store._stage

        def change(*args):
            owned = stage(*args)
            self.store.writer_path.unlink()
            self.store.writer_path.write_bytes(b"replacement writer")
            return owned

        with patch.object(self.store, "_stage", side_effect=change):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_cleanup_incomplete"
            )
        self.assertEqual(self.store.writer_path.read_bytes(), b"replacement writer")
        self.assertFalse(self.store.path.exists())

    def test_replaced_root_and_its_files_are_preserved(self):
        stage = self.store._stage
        observed = []

        def change(*args):
            owned = stage(*args)
            saved = self.base / "saved"
            self.root.rename(saved)
            shutil.copytree(saved, self.root)
            observed.append(snapshot(self.base))
            return owned

        with patch.object(self.store, "_stage", side_effect=change):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_cleanup_incomplete"
            )
        self.assertEqual(snapshot(self.base), observed[0])

    def test_same_bytes_replacement_of_previous_lock_still_refuses(self):
        previous = replace(self.prepared.lock, project_id="old")
        self.write_lock(previous)
        stage = self.store._stage

        def change(*args):
            owned = stage(*args)
            self.store.path.unlink()
            self.write_lock(previous)
            return owned

        with patch.object(self.store, "_stage", side_effect=change):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_changed"
            )
        self.assertEqual(self.store.read(), previous)

    def test_first_publication_never_clobbers_a_concurrent_destination(self):
        link = os.link

        def collide(source, target, **kwargs):
            Path(target).write_bytes(b"concurrent destination")
            return link(source, target, **kwargs)

        with patch.object(storage.os, "link", side_effect=collide):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_write_failed"
            )
        self.assertEqual(self.store.path.read_bytes(), b"concurrent destination")
        self.assertFalse(self.store.writer_path.exists())

    def test_postpublication_input_drift_is_explicit_and_never_claims_success(self):
        revalidate = storage.require_repository_lock_inputs_unchanged

        def change(prepared):
            if self.store.path.exists():
                guide = self.root / ".literate/orchestration/docs/overview.md"
                guide.write_bytes(guide.read_bytes() + b"\nChanged root intent.\n")
            return revalidate(prepared)

        with patch.object(
            storage, "require_repository_lock_inputs_unchanged", side_effect=change
        ):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_postpublication_failed"
            )
        self.assertEqual(self.store.read(), self.prepared.lock)
        self.assertFalse(self.store.writer_path.exists())
        self.assert_refusal(
            lambda: prepare_repository_lock(self.root), "root_authority_invalid"
        )

    def test_postpublication_same_bytes_replacement_is_detected(self):
        revalidate = storage.require_repository_lock_inputs_unchanged

        def change(prepared):
            revalidate(prepared)
            if self.store.path.exists():
                content = self.store.path.read_bytes()
                self.store.path.unlink()
                self.store.path.write_bytes(content)

        with patch.object(
            storage, "require_repository_lock_inputs_unchanged", side_effect=change
        ):
            self.assert_refusal(
                lambda: self.store.update(self.prepared), "lock_postpublication_failed"
            )
        self.assertEqual(
            json.loads(self.store.path.read_bytes()), self.prepared.lock.to_dict()
        )
        self.assertFalse(self.store.writer_path.exists())
