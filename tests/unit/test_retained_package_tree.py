"""Package custody rejects unbound content and preserves foreign replacements."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.directory_artifacts import DirectoryExportFile
from literate_ai.adapters.retained_package_tree import write_staged_package


class RetainedPackageTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.files = (DirectoryExportFile("source/lib.rs", b"pub fn x() {}", 0o644),)

    def test_exact_tree_survives_relocation(self):
        stage = self.root / "stage"
        stage.mkdir()
        tree = write_staged_package(stage, self.files)
        destination = self.root / "published"
        stage.rename(destination)
        tree.relocated(destination).require_unchanged()

    def test_extra_empty_directory_invalidates_custody(self):
        tree = write_staged_package(self.root, self.files)
        (self.root / "extra").mkdir()
        with self.assertRaisesRegex(ValueError, "unbound-directory"):
            tree.require_unchanged()

    def test_identical_bytes_replacement_invalidates_physical_custody(self):
        tree = write_staged_package(self.root, self.files)
        path = self.root / self.files[0].path
        replacement = self.root / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o644)
        replacement.replace(path)
        with self.assertRaisesRegex(ValueError, "custody-changed"):
            tree.require_unchanged()

    def test_hardlinked_file_is_refused(self):
        tree = write_staged_package(self.root, self.files)
        outside = self.root.parent / (self.root.name + "-link")
        try:
            os.link(self.root / self.files[0].path, outside)
        except OSError:
            self.skipTest("host does not support hard links")
        self.addCleanup(outside.unlink)
        with self.assertRaisesRegex(ValueError, "file-unsafe"):
            tree.require_unchanged()

    def test_invalid_path_refuses_before_writing(self):
        for name in ("../escape", "/absolute", "source/../escape"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                write_staged_package(
                    self.root, (DirectoryExportFile(name, b"x", 0o644),)
                )
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX file descriptor mode semantics")
    def test_permissions_apply_to_open_file_not_replacement(self):
        original = os.fchmod
        path = self.root / self.files[0].path

        def replace_before_mode(descriptor, mode):
            path.rename(path.with_suffix(".old"))
            path.write_bytes(b"foreign")
            path.chmod(0o600)
            original(descriptor, mode)

        with patch(
            "literate_ai.adapters.retained_package_tree.os.fchmod",
            side_effect=replace_before_mode,
        ):
            with self.assertRaises(ValueError):
                write_staged_package(self.root, self.files)
        self.assertEqual(path.read_bytes(), b"foreign")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
