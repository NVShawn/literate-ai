"""Evidence bytes must be bounded by their admitted immutable reference."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.storage import BlobIntegrityError, FileSystemCAS


class CasReadBoundsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cas = FileSystemCAS(Path(temporary.name) / "cas")

    def copy(self, reference):
        self.cas.copy_to(reference, self.cas.root.parent / "copied")

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
