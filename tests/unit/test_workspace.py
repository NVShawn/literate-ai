"""Whole-tree transactional acceptance tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.workspace import WorkspaceError, WorkspaceTreeStore


class WorkspaceTreeStoreTests(unittest.TestCase):
    def test_reference_compare_and_swap_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceTreeStore(Path(directory))
            first = store.prepare({"a": b"1"})
            store.commit(first, "component")
            second = store.prepare({"a": b"2"})
            with self.assertRaisesRegex(WorkspaceError, "changed before commit"):
                store.commit(second, "component", expected_tree_digest="sha256:stale")

    def test_paths_cannot_escape_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceTreeStore(Path(directory))
            unsafe = (
                "../outside",
                "source/../outside",
                "source\\outside",
                "C:source/main.py",
                "C:/source/main.py",
                "/source/main.py",
                "//?/C:/source/main.py",
                "source/NUL.txt",
                "source/COM¹.txt",
                "source/file:stream.py",
                "source/trailing.",
            )
            for path in unsafe:
                with self.subTest(path=path):
                    with self.assertRaises(WorkspaceError) as rejected:
                        store.prepare({path: b"bad"})
                    self.assertEqual(rejected.exception.code, "workspace.path_invalid")
            self.assertEqual(store.recover(), ())


if __name__ == "__main__":
    unittest.main()
