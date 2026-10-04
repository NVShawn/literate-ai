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


if __name__ == "__main__":
    unittest.main()
