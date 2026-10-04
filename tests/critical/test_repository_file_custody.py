"""Physical rollback inputs and foreign-entry refusal without worktree writes."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters import repository_file_custody as custody
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from tests.support.fixtures_test_repository_tree import tree


class RepositoryFileCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        node = self.root.stat()
        self.root_node = (node.st_dev, node.st_ino, node.st_mode)

    def write(self, path, content=b"old\n"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def prepare(self, before, after, **options):
        return custody.prepare_worktree_changes(
            self.root, self.root_node, tree(before), tree(after), **options
        )

    def refuses(self, suffix, action):
        with self.assertRaises(OrchestrationInventoryError) as caught:
            action()
        self.assertEqual(caught.exception.code, "orchestration.refresh_files_" + suffix)

    def test_addition_cannot_clobber_foreign_file_or_empty_directory(self):
        for kind in ("file", "directory"):
            with self.subTest(kind=kind):
                path = self.root / kind
                path.write_bytes(b"foreign") if kind == "file" else path.mkdir()
                self.refuses(
                    "collision",
                    lambda kind=kind: self.prepare({}, {kind: ("100644", b"new")}),
                )
                self.assertTrue(path.exists())

    def test_unowned_link_ancestor_refuses(self):
        self.write("outside/old")
        try:
            (self.root / "dir").symlink_to("outside", target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symlink fixtures")
        self.refuses(
            "unsafe",
            lambda: self.prepare(
                {"dir/old": ("100644", b"old\n")},
                {"dir/old": ("100644", b"new")},
            ),
        )

    def test_hardlinked_and_special_nodes_refuse(self):
        source = self.write("source")
        try:
            os.link(source, self.root / "alias")
        except OSError:
            self.skipTest("host does not permit hardlink fixtures")
        self.refuses(
            "unsafe", lambda: self.prepare({"source": ("100644", b"old\n")}, {})
        )
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "pipe")
            self.refuses("unsafe", lambda: self.prepare({"pipe": ("100644", b"")}, {}))

    def test_foreign_sibling_drift_invalidates_plan_without_removal(self):
        self.write("source")
        prepared = self.prepare({"source": ("100644", b"old\n")}, {})
        foreign = self.write("ignored", b"foreign")
        self.refuses(
            "changed", lambda: custody.require_worktree_changes_unchanged(prepared)
        )
        self.assertEqual(foreign.read_bytes(), b"foreign")
