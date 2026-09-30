"""Real native publication and deterministic conflicts; no package execution."""

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import exclusive_directory as publication


class ExclusiveDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xp-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "stage"
        self.destination = self.root / "packages"
        self.source.mkdir()
        (self.source / "Cargo.toml").write_bytes(b"reviewed manifest")
        (self.source / "src").mkdir()
        (self.source / "src/lib.rs").write_bytes(b"pub fn run() {}")
        self.arguments = {
            "expected_source": publication.directory_node(self.source),
            "expected_parent": publication.directory_node(self.root),
        }

    def publish(self):
        publication.publish_directory_exclusive(
            self.source, self.destination, **self.arguments
        )

    def test_native_publication_preserves_exact_tree_and_inode(self):
        self.publish()
        self.assertFalse(self.source.exists())
        self.assertEqual(
            publication.directory_node(self.destination),
            self.arguments["expected_source"],
        )
        self.assertEqual(
            (self.destination / "Cargo.toml").read_bytes(), b"reviewed manifest"
        )
        self.assertEqual(
            (self.destination / "src/lib.rs").read_bytes(), b"pub fn run() {}"
        )

    def test_existing_empty_directory_and_file_are_never_replaced(self):
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):
            self.publish()
        self.assertTrue(self.source.is_dir())
        self.assertTrue(self.destination.is_dir())
        self.destination.rmdir()
        self.destination.write_bytes(b"foreign")
        with self.assertRaises(FileExistsError):
            self.publish()
        self.assertEqual(self.destination.read_bytes(), b"foreign")
        self.assertTrue(self.source.is_dir())

    def test_directory_created_after_preflight_is_preserved_by_native_operation(self):
        original = os.rename if os.name == "nt" else publication._native_rename

        def concurrent(*args):
            self.destination.mkdir()
            return original(*args)

        target = (
            "os.rename"
            if os.name == "nt"
            else ("literate_ai.adapters.exclusive_directory._native_rename")
        )
        with patch(target, side_effect=concurrent), self.assertRaises(FileExistsError):
            self.publish()
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertTrue(self.source.is_dir())

    def test_stage_replacement_preserves_both_owners(self):
        original = self.root / "original"
        self.source.rename(original)
        self.source.mkdir()
        (self.source / "foreign").write_bytes(b"other owner")
        with self.assertRaisesRegex(ValueError, "stage-changed"):
            self.publish()
        self.assertFalse(self.destination.exists())
        self.assertEqual((self.source / "foreign").read_bytes(), b"other owner")
        self.assertEqual((original / "Cargo.toml").read_bytes(), b"reviewed manifest")

    def test_parent_identity_drift_refuses_before_publication(self):
        self.arguments["expected_parent"] = (0, 0, 0)
        with self.assertRaisesRegex(ValueError, "parent-changed"):
            self.publish()
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())

    def test_case_alias_and_link_destinations_are_preserved(self):
        alias = self.root / "PACKAGES"
        alias.mkdir()
        with self.assertRaises(FileExistsError):
            self.publish()
        self.assertTrue(alias.is_dir())
        alias.rmdir()
        try:
            self.destination.symlink_to(self.root / "missing", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(FileExistsError):
            self.publish()
        self.assertTrue(self.destination.is_symlink())

    def test_unsupported_native_rename_has_no_replacement_fallback(self):
        target = (
            "os.rename"
            if os.name == "nt"
            else ("literate_ai.adapters.exclusive_directory._native_rename")
        )
        with patch(target, side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with self.assertRaises(OSError) as error:
                self.publish()
        self.assertEqual(error.exception.errno, errno.ENOTSUP)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())

    def test_linked_parent_and_different_parents_refuse(self):
        outside = self.root / "other"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "path-invalid"):
            publication.publish_directory_exclusive(
                self.source, outside / "packages", **self.arguments
            )
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(RuntimeError):
            publication.publish_directory_exclusive(
                alias / "stage", alias / "packages", **self.arguments
            )
