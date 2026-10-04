"""Parent inode custody and normalization limited to live, owned additions."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_directories as directories
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.identity import canonical_identity
from tests.support import fixtures_test_write_reservations as fixtures


class RefreshDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WriteReservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.target = self.root / "logs/refs/heads/topic"

    def observe(self, owner=None):
        return directories.observe_metadata_parents(
            ((self.target, self.root, self.fixture.node),), owner
        )

    def test_missing_parents_are_read_only_and_only_owned_additions_normalize(self):
        before = self.observe()
        self.assertEqual(len(before), 4)
        self.assertTrue(all(item.node is None for item in before[1:]))
        self.assertFalse((self.root / "logs").exists())
        with self.fixture.acquire("logs/refs/heads/topic.lock") as owner:
            self.assertEqual(self.observe(owner), before)
            self.assertNotEqual(self.observe(), before)
            self.assertTrue(owner.owns_directory(self.target.parent))
            self.assertFalse(owner.owns_directory(self.root))
            owner.verify_all()
        self.assertEqual(self.observe(), before)
        with self.assertRaises(OrchestrationInventoryError):
            owner.owns_directory(self.target.parent)

    def test_existing_directory_identity_and_foreign_creation_change_custody(self):
        absent = self.observe()
        self.target.parent.mkdir(parents=True)
        existing = self.observe()
        self.assertNotEqual(existing, absent)
        with self.fixture.acquire("logs/refs/heads/topic.lock") as owner:
            self.assertFalse(owner.owns_directory(self.target.parent))
            self.assertEqual(self.observe(owner), existing)
        self.target.parent.rename(self.root / "retained-heads")
        self.target.parent.mkdir()
        self.assertNotEqual(self.observe(), existing)

    def test_directory_wire_preserves_oversized_os_node_identifiers(self):
        observation = directories.RefreshDirectoryObservation(
            self.root,
            (2**63, 2, 3),
        )

        wire = observation.to_dict()

        self.assertEqual(wire["node"][0], {"integer_decimal": str(2**63)})
        self.assertTrue(canonical_identity(wire).uri.startswith("sha256:"))

    @unittest.skipIf(os.name == "nt", "Windows cannot move an open CRT marker")
    def test_parent_swap_preserving_marker_inode_refuses_use_and_retains_cleanup(self):
        marker = self.target.with_name("topic.lock")
        retained = self.root / "retained-heads"
        with self.assertRaises(OrchestrationInventoryError):
            with self.fixture.acquire("logs/refs/heads/topic.lock") as owner:
                original = marker.stat().st_ino
                self.target.parent.rename(retained)
                self.target.parent.mkdir()
                (retained / marker.name).rename(marker)
                self.assertEqual(marker.stat().st_ino, original)
                for check in (owner.verify_all, lambda: self.observe(owner)):
                    with self.assertRaises(OrchestrationInventoryError):
                        check()
        self.assertTrue(marker.is_file())
        self.assertTrue(retained.is_dir())

    def test_optional_file_under_absent_parents_does_not_create_them(self):
        observed = directories.optional_metadata_file(
            self.target, self.root, self.fixture.node, maximum_bytes=100
        )
        self.assertIsNone(observed.content)
        self.assertIsNone(observed.signature)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_anchor_and_parent_depth_and_count_bounds_refuse(self):
        for limit, value in (("_MAX_DEPTH", 2), ("_MAX_DIRECTORIES", 1)):
            with patch.object(directories, limit, value):
                with self.assertRaises(OrchestrationInventoryError):
                    self.observe()
        with patch.object(directories, "_MAX_DIRECTORIES", 0):
            with self.assertRaises(OrchestrationInventoryError):
                directories.observe_metadata_parents(
                    ((self.root / "file", self.root, self.fixture.node),)
                )
        with self.assertRaises(OrchestrationInventoryError):
            directories.observe_metadata_parents(((self.target, self.root, (0, 0, 0)),))
