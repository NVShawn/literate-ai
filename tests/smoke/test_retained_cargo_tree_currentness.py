"""Fresh tree observations retain custody while avoiding duplicate CPU work."""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_cargo_tree_v2 as subject
from literate_ai.adapters.retained_project_inputs import _DirectoryAccess
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_cargo_custody import RetainedCargoInputBudget


class CargoTreeCurrentnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "source"
        self.directory.mkdir()
        self.payload = self.directory / "input.txt"
        self.payload.write_bytes(b"original payload")
        self.budget = RetainedCargoInputBudget(100, 200, 4096, 409600)

    def capture(self):
        return subject.capture_retained_cargo_tree_v2(
            self.root, budget=self.budget, maximum_metadata_bytes=32 * 1024**2
        )

    @unittest.skipIf(os.name == "nt", "POSIX anchored metadata count")
    def test_initial_census_metadata_is_not_reobserved_before_streaming(self):
        # One directory and N regular files: two censuses each observe N+2
        # nodes, streaming observes 3N, and final physical checks observe N+1.
        count = 8
        for index in range(count - 1):
            (self.directory / str(index)).write_bytes(b"payload")
        calls = []
        original = _DirectoryAccess.metadata

        def observed(access, path):
            calls.append(path)
            return original(access, path)

        with patch.object(_DirectoryAccess, "metadata", observed):
            tree = self.capture()
        self.assertEqual(tree.file_count, count)
        self.assertEqual(len(calls), 6 * count + 5)

    def test_currentness_compares_fresh_values_without_reencoding_both_trees(self):
        tree = self.capture()
        expected = tree.identity
        with patch.object(
            subject, "canonical_identity", wraps=subject.canonical_identity
        ) as canonical:
            tree.require_unchanged()
        self.assertEqual(canonical.call_count, 0)
        self.assertEqual(self.capture().identity, expected)

    def test_changed_content_refuses_on_next_independent_observation(self):
        tree = self.capture()
        self.payload.write_bytes(b"modified payload")
        with self.assertRaises(ValueError):
            tree.require_unchanged()

    def test_altered_expected_digest_cannot_hide_behind_same_physical_metadata(self):
        tree = self.capture()
        name, blob, executable = tree.files[0]
        changed = replace(
            tree, files=((name, BlobRef("0" * 64, blob.size), executable),)
        )
        with self.assertRaises(ValueError):
            changed.require_unchanged()

    @unittest.skipIf(os.name == "nt", "POSIX permission identity")
    def test_mode_change_refuses(self):
        tree = self.capture()
        self.payload.chmod(self.payload.stat().st_mode ^ 0o100)
        with self.assertRaises(ValueError):
            tree.require_unchanged()

    def test_parent_replacement_refuses_even_with_equal_bytes(self):
        tree = self.capture()
        self.directory.rename(self.root / "old")
        self.directory.mkdir()
        self.payload.write_bytes(b"original payload")
        with self.assertRaises(ValueError):
            tree.require_unchanged()

    def test_write_and_restore_after_initial_census_refuses(self):
        original = subject._inventory
        calls = 0
        before = self.payload.stat()

        def census(*args, **kwargs):
            nonlocal calls
            value = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                self.payload.write_bytes(b"temporary change")
                self.payload.write_bytes(b"original payload")
                os.utime(self.payload, ns=(before.st_atime_ns, before.st_mtime_ns))
            return value

        with patch.object(subject, "_inventory", census):
            with self.assertRaises(ValueError):
                self.capture()

    def test_write_after_closing_census_is_still_checked(self):
        original = subject._inventory
        calls = 0

        def census(*args, **kwargs):
            nonlocal calls
            value = original(*args, **kwargs)
            calls += 1
            if calls == 2:
                self.payload.write_bytes(b"late mutation!!!")
            return value

        with patch.object(subject, "_inventory", census):
            with self.assertRaises(ValueError):
                self.capture()


if __name__ == "__main__":
    unittest.main()
