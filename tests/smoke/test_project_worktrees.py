"""Canonical in-repository worktree placement tests with real Git metadata."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from literate_ai.adapters.project_worktrees import (
    resolve_project_worktree_location,
)
from literate_ai.cli.project import project_worktree_from_args


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


class ProjectWorktreeLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.name", "Literate AI test")
        _git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "tracked.txt").write_text("root\n", encoding="utf-8")
        _git(self.root, "add", "tracked.txt")
        _git(self.root, "commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_canonical_and_linked_checkouts_resolve_the_same_root(self) -> None:
        canonical = resolve_project_worktree_location(self.root)
        self.assertEqual(canonical.canonical_checkout, self.root.resolve())
        self.assertEqual(canonical.common_directory, self.root.resolve() / ".git")
        self.assertEqual(canonical.worktree_root, self.root.resolve() / ".worktrees")

        linked = self.root / ".worktrees" / "existing"
        linked.parent.mkdir()
        _git(self.root, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
        from_linked = resolve_project_worktree_location(linked)
        self.assertEqual(from_linked.canonical_checkout, canonical.canonical_checkout)
        self.assertEqual(from_linked.worktree_root, canonical.worktree_root)
        self.assertNotEqual(
            from_linked.registration_identity, canonical.registration_identity
        )

    def test_cli_reports_the_same_public_location_contract(self) -> None:
        report = project_worktree_from_args(
            Namespace(worktree_action="location", path=str(self.root))
        )
        self.assertEqual(
            report["worktree_root"], str(self.root.resolve() / ".worktrees")
        )
        self.assertTrue(report["placement_enforced"])
        self.assertTrue(str(report["location_identity"]).startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
