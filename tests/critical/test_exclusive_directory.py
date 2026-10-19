"""Real native publication and deterministic conflicts; no package execution."""

import errno
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_native_success_preserves_the_exact_staged_directory(self):
        self.publish()
        self.assertFalse(self.source.exists())
        self.assertEqual(
            publication.directory_node(self.destination),
            self.arguments["expected_source"],
        )
        self.assertEqual(
            (self.destination / "Cargo.toml").read_bytes(), b"reviewed manifest"
        )

    @unittest.skipIf(os.name == "nt", "POSIX descriptor rename boundary")
    def test_destination_created_after_prechecks_is_not_replaced(self):
        original = publication._native_rename

        def race(parent, source, destination):
            self.destination.mkdir()
            node = publication.directory_node(self.destination)
            with self.assertRaises(FileExistsError):
                original(parent, source, destination)
            self.assertEqual(publication.directory_node(self.destination), node)
            raise FileExistsError(errno.EEXIST, "concurrent destination preserved")

        with patch.object(publication, "_native_rename", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.publish()
        self.assertTrue(self.source.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_freebsd_uses_the_native_no_replace_flag_and_refuses_missing_symbol(self):
        function = Mock(return_value=0)
        with patch.object(publication.sys, "platform", "freebsd16"):
            with patch.object(
                publication.ctypes,
                "CDLL",
                return_value=SimpleNamespace(renameat2=function),
            ):
                publication._native_rename(7, b"stage", b"packages")
            function.assert_called_once_with(7, b"stage", 7, b"packages", 1)
            with patch.object(
                publication.ctypes, "CDLL", return_value=SimpleNamespace()
            ):
                with self.assertRaises(OSError) as caught:
                    publication._native_rename(7, b"stage", b"packages")
            self.assertEqual(caught.exception.errno, errno.ENOTSUP)

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
