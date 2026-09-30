"""Whole-tree transactional acceptance tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.workspace import WorkspaceError, WorkspaceTreeStore


class WorkspaceTreeStoreTests(unittest.TestCase):
    def test_commit_switches_one_reference_after_complete_tree_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceTreeStore(Path(directory))
            first = store.prepare({"src/a.py": b"first", "src/b.py": b"same"})
            first_path = store.commit(first, "components/example")
            self.assertEqual(store.resolve("components/example"), first_path)
            second = store.prepare({"src/a.py": b"second", "src/b.py": b"same"})
            second_path = store.commit(
                second,
                "components/example",
                expected_tree_digest=first.tree_digest,
            )
            self.assertNotEqual(first_path, second_path)
            self.assertEqual((second_path / "src/a.py").read_bytes(), b"second")

    def test_failed_precondition_never_creates_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceTreeStore(Path(directory))

            def fail(_: Path) -> None:
                raise RuntimeError("validation failed")

            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                store.prepare({"source.py": b"bad"}, precondition=fail)
            self.assertIsNone(store.resolve("components/example"))
            self.assertEqual(store.recover(), ())

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

    def test_paths_cannot_alias_or_overlap_on_supported_filesystems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceTreeStore(Path(directory))
            invalid_trees = (
                {"source/A.py": b"first", "source/a.py": b"second"},
                {"source/package": b"file", "source/package/module.py": b"child"},
                {"source/café.py": b"first", "source/cafe\u0301.py": b"second"},
            )
            for files in invalid_trees:
                with self.subTest(files=tuple(files)):
                    with self.assertRaises(WorkspaceError) as rejected:
                        store.prepare(files)
                    self.assertEqual(rejected.exception.code, "workspace.path_invalid")
            self.assertEqual(store.recover(), ())

    def test_canonical_source_paths_materialize_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceTreeStore(Path(directory))
            prepared = store.prepare(
                {
                    "source/main.py": b"print('ok')\n",
                    "source/data/input #1.txt": b"one\n",
                }
            )

            committed = store.commit(prepared, "generated/application")

            self.assertEqual(
                tuple(path for path, _digest in prepared.manifest),
                ("source/data/input #1.txt", "source/main.py"),
            )
            self.assertEqual(
                (committed / "source/data/input #1.txt").read_bytes(), b"one\n"
            )

    def test_rewritten_source_and_manifest_cannot_relabel_a_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceTreeStore(Path(directory))
            prepared = store.prepare({"source/main.py": b"original\n"})
            committed = store.commit(prepared, "generated/application")
            changed = b"changed\n"
            (committed / "source/main.py").write_bytes(changed)
            (committed / ".literate-tree.json").write_text(
                json.dumps(
                    {
                        "tree_digest": prepared.tree_digest,
                        "files": [
                            [
                                "source/main.py",
                                "sha256:" + hashlib.sha256(changed).hexdigest(),
                            ]
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

            with self.assertRaises(WorkspaceError) as rejected:
                store.resolve("generated/application")

            self.assertEqual(rejected.exception.code, "workspace.tree_collision")


if __name__ == "__main__":
    unittest.main()
