"""Monorepo custody must validate the opened file before reading its contents."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from literate_ai.adapters import monorepo_adoption as adoption
from literate_ai.adapters import monorepo_components as components


class MonorepoInputReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="litai input custody ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "input.json"
        self.content = json.dumps(
            {
                "schema": adoption.SELECTION_SCHEMA,
                "components": [],
                "shared_sources": [],
            }
        ).encode()
        self.path.write_bytes(self.content)

    def readers(self):
        return (
            lambda: adoption._selection(self.path),
            lambda: adoption._source_members(self.root, [self.path.name]),
            lambda: components._read(self.path),
        )

    def intercept_open(self, stack, replacement):
        """Exercise both the old Path opener and the descriptor-based repair."""
        path_open, descriptor_open = Path.open, os.open
        fired = []

        def before(path):
            if path == self.path and not fired:
                fired.append(True)
                replacement()

        def open_path(path, *args, **kwargs):
            before(path)
            return path_open(path, *args, **kwargs)

        def open_descriptor(path, *args, **kwargs):
            before(path)
            return descriptor_open(path, *args, **kwargs)

        stack.enter_context(mock.patch.object(Path, "open", open_path))
        stack.enter_context(mock.patch.object(os, "open", open_descriptor))
        return fired

    def test_replacement_before_open_is_rejected_before_hashing(self):
        for reader in self.readers():
            with self.subTest(reader=reader), ExitStack() as stack:
                self.path.write_bytes(self.content)
                replacement = self.root / "replacement.json"
                replacement.write_bytes(self.content + b" ")
                fired = self.intercept_open(
                    stack,
                    lambda replacement=replacement: replacement.replace(self.path),
                )
                digest = mock.Mock(wraps=hashlib.sha256())
                stack.enter_context(
                    mock.patch.object(adoption.hashlib, "sha256", return_value=digest)
                )
                with self.assertRaises(adoption.MonorepoAdoptionError):
                    reader()
                self.assertEqual(fired, [True])
                digest.update.assert_not_called()

    def test_raw_bytes_and_source_identity_are_unchanged(self):
        content = "λ\r\n☃\n".encode() + b"\x00\xff"
        self.path.write_bytes(content)
        before = self.path.stat()
        self.assertEqual(components._read(self.path), content)
        self.assertEqual(
            adoption._source_members(self.root, [self.path.name]),
            [
                {
                    "path": self.path.name,
                    "identity": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "executable": bool(before.st_mode & 0o111),
                }
            ],
        )
        self.assertEqual(self.path.read_bytes(), content)

    def test_selection_rejects_indirect_parent(self):
        indirect = self.root / "link"
        try:
            indirect.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(adoption.MonorepoAdoptionError):
            adoption._selection(indirect / self.path.name)

    def test_document_size_bounds_still_apply(self):
        with mock.patch.object(components, "_MAX_DOCUMENT", 4):
            with self.assertRaises(adoption.MonorepoAdoptionError):
                components._read(self.path)
        with mock.patch.object(adoption, "_MAX_SELECTION_BYTES", 4):
            with self.assertRaises(adoption.MonorepoAdoptionError):
                adoption._selection(self.path)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_fifo_replacement_does_not_block_or_read_and_closes_descriptor(self):
        for reader in self.readers():
            with self.subTest(reader=reader):
                self.path.unlink()
                self.path.write_bytes(self.content)
                original_open = os.open
                opened = []

                def replace_with_fifo(
                    path,
                    flags,
                    *args,
                    original_open=original_open,
                    opened=opened,
                    **kwargs,
                ):
                    if path == self.path:
                        self.assertTrue(flags & os.O_NONBLOCK)
                        self.path.unlink()
                        os.mkfifo(self.path)
                    descriptor = original_open(path, flags, *args, **kwargs)
                    if path == self.path:
                        opened.append(descriptor)
                    return descriptor

                with (
                    mock.patch.object(os, "open", replace_with_fifo),
                    mock.patch.object(os, "read") as read,
                ):
                    with self.assertRaises(adoption.MonorepoAdoptionError):
                        reader()
                    read.assert_not_called()
                self.assertEqual(len(opened), 1)
                with self.assertRaises(OSError):
                    os.fstat(opened[0])

    def test_symlink_replacement_does_not_read_its_target(self):
        foreign = self.root / "foreign.json"
        foreign.write_bytes(b"foreign source")
        link = self.root / "link.json"
        try:
            link.symlink_to(foreign)
        except OSError as exc:
            self.skipTest(str(exc))
        link.unlink()
        for reader in self.readers():
            with self.subTest(reader=reader), ExitStack() as stack:
                self.path.unlink()
                self.path.write_bytes(self.content)
                link.symlink_to(foreign)
                self.intercept_open(stack, lambda: link.replace(self.path))
                read = stack.enter_context(mock.patch.object(os, "read"))
                with self.assertRaises(adoption.MonorepoAdoptionError):
                    reader()
                read.assert_not_called()
        self.assertEqual(foreign.read_bytes(), b"foreign source")

    def test_changes_during_reads_are_rejected_and_not_rolled_back(self):
        for reader in self.readers():
            with self.subTest(reader=reader):
                self.path.write_bytes(self.content)
                read = os.read
                changed = []

                def change_after_read(descriptor, size, read=read, changed=changed):
                    chunk = read(descriptor, size)
                    if not changed:
                        self.path.write_bytes(b"replacement kept")
                        changed.append(True)
                    return chunk

                with mock.patch.object(os, "read", change_after_read):
                    with self.assertRaises(adoption.MonorepoAdoptionError):
                        reader()
                self.assertEqual(self.path.read_bytes(), b"replacement kept")

    def test_growth_is_bounded_by_initial_source_size(self):
        initial = b"x" * (1024 * 1024 + 100)
        self.path.write_bytes(initial)
        read = os.read
        requested = []

        def growing_read(descriptor, size):
            requested.append(size)
            with self.path.open("ab") as stream:
                stream.write(b"y" * 1024)
            return read(descriptor, size)

        with mock.patch.object(os, "read", growing_read):
            with self.assertRaisesRegex(adoption.MonorepoAdoptionError, "grew"):
                adoption._source_members(self.root, [self.path.name])
        self.assertEqual(sum(requested), len(initial) + 1)
        self.assertEqual(self.path.stat().st_size, len(initial) + 2048)

    def test_empty_source_still_has_the_original_byte_identity(self):
        self.path.write_bytes(b"")
        self.assertEqual(components._read(self.path), b"")
        self.assertEqual(
            adoption._source_members(self.root, [self.path.name])[0]["identity"],
            "sha256:" + hashlib.sha256(b"").hexdigest(),
        )

    def test_consumer_failure_closes_the_opened_descriptor(self):
        original_open = os.open
        opened = []

        def observe_open(path, *args, **kwargs):
            descriptor = original_open(path, *args, **kwargs)
            if path == self.path:
                opened.append(descriptor)
            return descriptor

        with mock.patch.object(os, "open", observe_open):
            with self.assertRaisesRegex(RuntimeError, "consumer failed"):
                adoption._consume_direct_file(
                    self.path, mock.Mock(side_effect=RuntimeError("consumer failed"))
                )
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])


if __name__ == "__main__":
    unittest.main()
