"""Root-only lock writes, no-write inspection and concurrent-change preservation."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_locks as storage
from literate_ai.adapters.repository_lock_planning import prepare_repository_lock
from literate_ai.adapters.repository_orchestration import (
    OrchestrationInventoryError,
)
from literate_ai.contracts.identity import canonical_json_bytes
from tests.support import fixtures_test_repository_lock_planning as fixtures
from tests.support.fixtures_test_repository_orchestration import snapshot
from tests.support.symlinks import skip_unless_symlinks_followable


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

    def test_link_destination_does_not_modify_its_target(self):
        target = self.base / "foreign.json"
        target.write_bytes(b"foreign")
        skip_unless_symlinks_followable(self, self.base)
        try:
            self.store.path.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        before = snapshot(self.base)
        self.assert_refusal(lambda: self.store.update(self.prepared), "lock_invalid")
        self.assertEqual(snapshot(self.base), before)

    def test_existing_writer_marker_is_preserved_and_fails_fast(self):
        self.store.writer_path.write_bytes(b"other writer")
        before = snapshot(self.base)
        self.assert_refusal(lambda: self.store.update(self.prepared), "lock_busy")
        self.assertEqual(snapshot(self.base), before)

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
