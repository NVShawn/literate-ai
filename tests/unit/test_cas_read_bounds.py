"""Evidence bytes must be bounded by their admitted immutable reference."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.storage import BlobIntegrityError, FileSystemCAS


class CasReadBoundsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cas = FileSystemCAS(Path(temporary.name) / "cas")

    def copy(self, reference):
        self.cas.copy_to(reference, self.cas.root.parent / "copied")

    def test_wrong_size_is_rejected_before_reading_any_file_content(self):
        reference = self.cas.put_bytes(b"evidence")
        self.cas.path_for(reference).write_bytes(b"x" * (2 * 1024 * 1024))
        for operation in (self.cas.get_bytes, self.cas.verify, self.copy):
            with self.subTest(operation=operation.__name__):
                with mock.patch(
                    "literate_ai.storage.cas.os.fdopen", wraps=os.fdopen
                ) as opened:
                    with self.assertRaises(BlobIntegrityError):
                        operation(reference)
                opened.assert_not_called()

    def test_file_growth_after_stat_cannot_force_an_unbounded_read(self):
        reference = self.cas.put_bytes(b"evidence")
        path = self.cas.path_for(reference)
        real_fdopen = os.fdopen
        for operation in (self.cas.get_bytes, self.cas.verify, self.copy):
            path.write_bytes(b"evidence")
            read_sizes = []

            @contextmanager
            def grow_after_stat(*args, observed_sizes=read_sizes, **kwargs):
                with real_fdopen(*args, **kwargs) as stream:
                    with path.open("ab") as output:
                        output.write(b"x" * (2 * 1024 * 1024))

                    def read(size):
                        content = stream.read(size)
                        observed_sizes.append(len(content))
                        return content

                    yield SimpleNamespace(read=read)

            with self.subTest(operation=operation.__name__):
                with mock.patch(
                    "literate_ai.storage.cas.os.fdopen", side_effect=grow_after_stat
                ):
                    with self.assertRaises(BlobIntegrityError):
                        operation(reference)
                self.assertLessEqual(sum(read_sizes), reference.size + 1)
                self.assertFalse((self.cas.root.parent / "copied").exists())

    def test_exact_size_still_requires_the_expected_digest(self):
        reference = self.cas.put_bytes(b"evidence")
        self.cas.path_for(reference).write_bytes(b"tampered")
        with self.assertRaises(BlobIntegrityError):
            self.cas.get_bytes(reference)
        with self.assertRaises(BlobIntegrityError):
            self.cas.verify(reference)
        with self.assertRaises(BlobIntegrityError):
            self.copy(reference)
        self.assertFalse((self.cas.root.parent / "copied").exists())

    def test_empty_and_multiple_chunk_blobs_remain_readable(self):
        for content in (b"", b"evidence" * (256 * 1024)):
            with self.subTest(size=len(content)):
                reference = self.cas.put_bytes(content)
                self.assertEqual(self.cas.get_bytes(reference), content)
                self.cas.verify(reference)
                output = self.cas.root.parent / f"copy-{len(content)}"
                self.cas.copy_to(reference, output)
                self.assertEqual(output.read_bytes(), content)

    def test_copy_never_overwrites_an_existing_destination(self):
        reference = self.cas.put_bytes(b"evidence")
        output = self.cas.root.parent / "existing"
        output.write_bytes(b"owned by another operation")
        with self.assertRaises(FileExistsError):
            self.cas.copy_to(reference, output)
        self.assertEqual(output.read_bytes(), b"owned by another operation")

    @unittest.skipIf(os.name == "nt", "Windows does not permit replacing an open file")
    def test_copy_rejects_replaced_destination_and_preserves_its_new_owner(self):
        reference = self.cas.put_bytes(b"evidence")
        output = self.cas.root.parent / "replaced"
        original_read = self.cas._read_verified

        def replace_after_read(*args, **kwargs):
            result = original_read(*args, **kwargs)
            output.unlink()
            output.write_bytes(b"owned by another operation")
            return result

        with mock.patch.object(
            self.cas, "_read_verified", side_effect=replace_after_read
        ):
            with self.assertRaisesRegex(RuntimeError, "destination was replaced"):
                self.cas.copy_to(reference, output)
        self.assertEqual(output.read_bytes(), b"owned by another operation")
