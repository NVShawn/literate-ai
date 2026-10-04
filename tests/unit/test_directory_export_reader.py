"""Adversarial admission of the existing directory ZIP format without extraction."""

from __future__ import annotations

import hashlib
import io
import stat
import unittest
import warnings
import zipfile

from literate_ai.adapters.directory_artifacts import (
    read_directory_export,
)
from literate_ai.contracts.blobs import BlobRef


def _archive(
    names: tuple[str, ...],
    *,
    mode: int = stat.S_IFREG | 0o644,
    compression: int = zipfile.ZIP_STORED,
) -> bytes:
    stream = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(stream, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            # ZipInfo normalizes Windows separators. Adversarial fixtures must
            # retain the requested wire name instead of becoming safe on Windows.
            info.filename = name
            info.orig_filename = name
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = compression
            archive.writestr(info, b"package data")
    return stream.getvalue()


def _read(content: bytes, **limits: int):
    return read_directory_export(
        content,
        BlobRef(hashlib.sha256(content).hexdigest(), len(content)),
        max_bytes=limits.get("max_bytes", 1024 * 1024),
        max_entries=limits.get("max_entries", 20),
    )


class DirectoryExportReaderTests(unittest.TestCase):
    def test_rejects_unsafe_paths_and_collisions(self):
        for names in (
            ("../a",),
            ("/a",),
            ("C:a",),
            ("a\\b",),
            ("NUL.txt",),
            ("a.",),
            ("a", "a"),
            ("A", "a"),
            ("a", "a/b"),
            ("z", "a"),
            ("a/",),
            ("A/one", "a/two"),
        ):
            with self.subTest(names=names):
                content = _archive(names)
                if names == ("a\\b",):
                    self.assertEqual(content.count(b"a\\b"), 2)
                with self.assertRaises(ValueError):
                    _read(content)

    def test_rejects_links_devices_privileged_modes_and_compression(self):
        for mode in (
            stat.S_IFLNK | 0o777,
            stat.S_IFCHR | 0o644,
            stat.S_IFDIR | 0o755,
            stat.S_IFREG | 0o4755,
        ):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                _read(_archive(("a",), mode=mode))
        with self.assertRaises(ValueError):
            _read(_archive(("a",), compression=zipfile.ZIP_DEFLATED))
