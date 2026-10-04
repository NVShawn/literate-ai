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
