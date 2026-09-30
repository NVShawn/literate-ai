"""A read-only cache lookup must not create a root or acquire a writing lock."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.storage import FileSystemCAS, ReferenceIndex, StorageError, indexes


class ReadOnlyReferenceIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cas = FileSystemCAS(self.root / "cas")
        self.blob = self.cas.put_bytes(b"verified artifact")
        self.index = ReferenceIndex(self.root / "index", self.cas)
        self.record = self.index.set("html", "key", self.blob)

    def test_read_only_resolves_and_lists_without_any_creation_or_lock(self):
        before = {p: p.stat() for p in self.root.rglob("*")}
        with (
            mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")),
            mock.patch.object(indexes, "FileLock", side_effect=AssertionError("lock")),
        ):
            index = ReferenceIndex(self.index.root, self.cas, read_only=True)
            self.assertEqual(index.resolve("html", "key"), self.record)
            self.assertEqual(index.list(), (self.record,))
            self.assertIsNone(index.resolve("html", "missing"))
        self.assertEqual(set(before), set(self.root.rglob("*")))
        for path, previous in before.items():
            self.assertEqual(path.stat().st_mtime_ns, previous.st_mtime_ns)

    def test_absent_document_is_a_non_writing_miss(self):
        self.index.path.unlink()
        index = ReferenceIndex(self.index.root, self.cas, read_only=True)
        self.assertIsNone(index.resolve("html", "key"))
        self.assertFalse(index.path.exists())

    def test_absent_root_is_not_created(self):
        root = self.root / "absent"
        with self.assertRaises(StorageError):
            ReferenceIndex(root, self.cas, read_only=True)
        self.assertFalse(root.exists())

    def test_set_is_refused_even_when_all_directories_exist(self):
        index = ReferenceIndex(self.index.root, self.cas, read_only=True)
        before = self.index.path.read_bytes()
        with self.assertRaisesRegex(StorageError, "read-only"):
            index.set("html", "key", self.blob)
        self.assertEqual(self.index.path.read_bytes(), before)

    def test_malformed_index_and_corrupt_blob_are_not_cache_misses(self):
        index = ReferenceIndex(self.index.root, self.cas, read_only=True)
        self.cas.path_for(self.blob).write_bytes(b"corruption")
        with self.assertRaises(StorageError):
            index.resolve("html", "key")
        self.index.path.write_bytes(b"[]")
        with self.assertRaises(StorageError):
            index.resolve("html", "key")

    def test_snapshot_limit_refuses_before_opening(self):
        index = ReferenceIndex(self.index.root, self.cas, read_only=True)
        with (
            mock.patch.object(indexes, "_MAXIMUM_REFERENCE_SNAPSHOT_BYTES", 1),
            mock.patch.object(indexes.os, "open", wraps=os.open) as opened,
        ):
            with self.assertRaisesRegex(StorageError, "byte limit"):
                index.resolve("html", "key")
        self.assertFalse(
            any(call.args[0] == self.index.path for call in opened.call_args_list)
        )

    def test_atomic_replacement_during_read_is_not_a_miss(self):
        index = ReferenceIndex(self.index.root, self.cas, read_only=True)
        original_read = os.read
        original_lstat = Path.lstat
        replacement = self.index.root / "replacement.json"
        replacement.write_bytes(self.index.path.read_bytes())
        replacement_metadata = replacement.lstat()
        read_started = False

        def publish(descriptor, maximum):
            nonlocal read_started
            content = original_read(descriptor, maximum)
            read_started = True
            return content

        def named_metadata(path, *args, **kwargs):
            # Model the new named inode without requiring Windows to rename an
            # open file; the real descriptor and its observed bytes stay intact.
            if path == self.index.path and read_started:
                return replacement_metadata
            return original_lstat(path, *args, **kwargs)

        with (
            mock.patch.object(indexes.os, "read", side_effect=publish),
            mock.patch.object(Path, "lstat", named_metadata),
        ):
            with self.assertRaisesRegex(StorageError, "changed while reading"):
                index.resolve("html", "key")
        self.assertEqual(index.resolve("html", "key"), self.record)

    def test_read_error_closes_the_opened_descriptor(self):
        index = ReferenceIndex(self.index.root, self.cas, read_only=True)
        descriptors = []

        def fail_read(descriptor, maximum):
            descriptors.append(descriptor)
            raise OSError("fixture failure")

        with mock.patch.object(indexes.os, "read", side_effect=fail_read):
            with self.assertRaisesRegex(StorageError, "unreadable"):
                index.resolve("html", "key")
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
