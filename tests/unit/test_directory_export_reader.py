"""Adversarial admission of the existing directory ZIP format without extraction."""

from __future__ import annotations

import hashlib
import io
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    directory_export_bytes,
    encode_directory_export,
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
    def test_memory_writer_preserves_existing_bytes_after_source_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payload").write_bytes(b"retained package bytes")
            (root / "record.json").write_bytes(b'{"evidence":"exact"}')
            expected = directory_export_bytes(root)
            files = _read(expected)
        self.assertFalse(root.exists())
        encoded = encode_directory_export(
            tuple(reversed(files)), max_bytes=len(expected), max_entries=len(files)
        )
        self.assertEqual(encoded, expected)
        self.assertEqual(_read(encoded), files)

    def test_memory_writer_bounds_archive_overhead_before_allocating(self):
        files = (DirectoryExportFile("record", b"data", 0o644),)
        size = 22 + 76 + 2 * len("record") + len(b"data")
        with patch("literate_ai.adapters.directory_artifacts.io.BytesIO") as allocate:
            with self.assertRaisesRegex(ValueError, "byte bound"):
                encode_directory_export(files, max_bytes=size - 1, max_entries=1)
            allocate.assert_not_called()
        self.assertEqual(
            len(encode_directory_export(files, max_bytes=size, max_entries=1)), size
        )

    def test_memory_writer_rejects_unsafe_files_and_aliases(self):
        for files in (
            (DirectoryExportFile("../escape", b"x", 0o644),),
            (DirectoryExportFile("safe", bytearray(b"x"), 0o644),),
            (DirectoryExportFile("safe", b"x", 0o4755),),
            (DirectoryExportFile("same", b"x", 0o644),) * 2,
            (
                DirectoryExportFile("Dir/a", b"x", 0o644),
                DirectoryExportFile("dir/b", b"x", 0o644),
            ),
        ):
            with self.subTest(files=files), self.assertRaises(ValueError):
                encode_directory_export(files, max_bytes=4096, max_entries=10)

    def test_existing_producer_roundtrips_content_and_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src/lib.rs").write_bytes(b"pub fn value() -> u8 { 7 }")
            (root / "tool").write_bytes(b"never execute me")
            (root / "tool").chmod(0o755)
            files = _read(directory_export_bytes(root))
            self.assertEqual([item.path for item in files], ["src/lib.rs", "tool"])
            for item in files:
                self.assertEqual(item.content, (root / item.path).read_bytes())
                self.assertEqual(
                    item.mode, stat.S_IMODE((root / item.path).stat().st_mode)
                )

    def test_pinned_bytes_and_limits_are_required(self):
        content = _archive(("a", "b"))
        expected = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
        for value in (content[:-1], content + b"x", bytes(len(content))):
            with self.subTest(value=len(value)), self.assertRaises(ValueError):
                read_directory_export(value, expected, max_bytes=10000, max_entries=20)
        for limits in (
            {"max_bytes": len(content) - 1},
            {"max_entries": 1},
            {"max_entries": True},
            {"max_bytes": 0},
        ):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                _read(content, **limits)

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

    def test_forged_entry_count_rejects_before_zipfile_allocates(self):
        content = bytearray(_archive(("a", "b")))
        struct.pack_into("<HH", content, len(content) - 22 + 8, 1, 1)
        with patch(
            "literate_ai.adapters.directory_artifacts.zipfile.ZipFile"
        ) as parser:
            with self.assertRaises(ValueError):
                _read(bytes(content))
            parser.assert_not_called()

    def test_noncanonical_headers_trailing_data_and_corruption_refuse(self):
        content = _archive(("a",))
        prefix = bytearray(content)
        # Preserve a parseable archive while changing only its local timestamp.
        prefix[10] ^= 1
        corrupt = bytearray(content)
        corrupt[31] ^= 1
        nul_name = bytearray(content)
        nul_name[30] = 0
        central = nul_name.index(b"PK\x01\x02")
        nul_name[central + 46] = 0
        for value in (
            b"",
            content[:-1],
            content + b"suffix",
            bytes(prefix),
            bytes(corrupt),
            bytes(nul_name),
        ):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                _read(value)
