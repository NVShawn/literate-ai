"""Index-format preflight must not let Git mutate shared-index timestamps."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters import repository_orchestration as inventory
from tests.unit.test_repository_orchestration import git, repository, snapshot


class RepositoryIndexObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "repository"
        repository(self.root)

    def test_real_v2_v3_v4_index_entries_and_extensions_remain_read_only(self):
        for name in ("link", "link-next", "long-" + "x" * 150, "short"):
            (self.root / name).write_bytes(b"ordinary source")
        git(self.root, "add", ".")
        for version in (2, 3, 4):
            with self.subTest(version=version):
                git(self.root, "update-index", "--index-version", str(version))
                if version > 2:
                    git(self.root, "update-index", "--skip-worktree", "source.txt")
                before = snapshot(self.root)
                self.assertEqual(
                    inventory.inspect_gitlink_inventory(self.root).children, ()
                )
                self.assertEqual(snapshot(self.root), before)
                git(self.root, "update-index", "--no-skip-worktree", "source.txt")

    def test_split_index_refusal_preserves_shared_file_bytes_and_mtimes(self):
        for version in (2, 4):
            with self.subTest(version=version):
                git(self.root, "update-index", "--index-version", str(version))
                git(self.root, "update-index", "--split-index")
                before = snapshot(self.root)
                with self.assertRaises(inventory.OrchestrationInventoryError) as caught:
                    inventory.inspect_gitlink_inventory(self.root)
                self.assertEqual(
                    caught.exception.code, "orchestration.split_index_unsupported"
                )
                self.assertEqual(snapshot(self.root), before)
                git(self.root, "update-index", "--no-split-index")

    def test_sha256_index_and_split_index_use_the_correct_oid_width(self):
        root = self.base / "sha256"
        repository(root, sha256=True)
        before = snapshot(root)
        self.assertEqual(inventory.inspect_gitlink_inventory(root).children, ())
        self.assertEqual(snapshot(root), before)
        git(root, "update-index", "--split-index")
        before = snapshot(root)
        with self.assertRaises(inventory.OrchestrationInventoryError) as caught:
            inventory.inspect_gitlink_inventory(root)
        self.assertEqual(caught.exception.code, "orchestration.split_index_unsupported")
        self.assertEqual(snapshot(root), before)

    def test_truncated_or_inconsistent_index_bytes_refuse_without_repair(self):
        index = self.root / ".git/index"
        original = index.read_bytes()
        bad_length = bytearray(original)
        bad_length[72:74] = b"\x0f\xfe"
        for content in (
            b"DIRC",
            original[:8] + b"\xff" * 4 + original[12:],
            bytes(bad_length),
            original[:10],
        ):
            with self.subTest(size=len(content)):
                index.write_bytes(content)
                before = snapshot(self.root)
                with self.assertRaises(inventory.OrchestrationInventoryError):
                    inventory.inspect_gitlink_inventory(self.root)
                self.assertEqual(snapshot(self.root), before)
