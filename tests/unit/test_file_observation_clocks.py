"""Path and descriptor clocks can differ but must each remain unchanged."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters import monorepo_adoption as adoption
from literate_ai.storage import indexes


class FileObservationClockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "document.json"
        self.content = b'{"schema_version":1,"references":{}}\r\n'
        self.path.write_bytes(self.content)

    def readers(self):
        return (
            (
                lambda: adoption._read_direct_bytes(self.path, 1024),
                self.content,
                adoption.MonorepoAdoptionError,
            ),
            (
                lambda: indexes._read_reference_snapshot(self.path),
                {"schema_version": 1, "references": {}},
                indexes.IndexError,
            ),
        )

    @staticmethod
    def metadata(node, clock):
        return SimpleNamespace(
            **{
                field: getattr(node, field)
                for field in ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns")
            },
            st_ctime_ns=clock,
            st_file_attributes=getattr(node, "st_file_attributes", 0),
        )

    def descriptor_clock(self, *, drift=False):
        fstat = os.fstat
        count = 0

        def observed(descriptor):
            nonlocal count
            node = fstat(descriptor)
            if not stat.S_ISREG(node.st_mode):
                return node
            count += 1
            return self.metadata(
                node, node.st_ctime_ns + (count if drift else 1) * 2_000_000_000
            )

        return mock.patch.object(os, "fstat", side_effect=observed)

    def test_distinct_stable_clocks_preserve_exact_reads(self):
        for reader, expected, _error in self.readers():
            with self.subTest(reader=reader), self.descriptor_clock():
                self.assertEqual(reader(), expected)
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_descriptor_clock_drift_refuses(self):
        for reader, _expected, error in self.readers():
            with (
                self.subTest(reader=reader),
                self.descriptor_clock(drift=True),
                self.assertRaisesRegex(error, "changed while reading"),
            ):
                reader()
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_path_clock_drift_refuses(self):
        lstat = Path.lstat
        for reader, _expected, error in self.readers():
            count = 0

            def observed(path, *args, **kwargs):
                nonlocal count
                node = lstat(path, *args, **kwargs)
                if path != self.path:
                    return node
                count += 1
                return self.metadata(
                    node, node.st_ctime_ns + (count > 2) * 2_000_000_000
                )

            with (
                self.subTest(reader=reader),
                mock.patch.object(Path, "lstat", new=observed),
                self.assertRaisesRegex(error, "changed while reading"),
            ):
                reader()
        self.assertEqual(self.path.read_bytes(), self.content)
