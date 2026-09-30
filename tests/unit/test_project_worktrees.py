"""Canonical in-repository worktree placement tests with real Git metadata."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from literate_ai.adapters.project_worktrees import (
    ProjectWorktreeError,
    reserve_project_worktree,
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

    def test_reservation_is_deterministic_atomic_and_does_not_fake_registration(
        self,
    ) -> None:
        with reserve_project_worktree(
            self.root,
            purpose="Release 1.2 integration",
            branch_or_revision="release/1.2",
        ) as reserved:
            self.assertEqual(reserved.path.parent, self.root.resolve() / ".worktrees")
            self.assertRegex(reserved.name, r"^release-1-2-integration-[0-9a-f]{12}$")
            self.assertFalse(reserved.path.exists())
            self.assertTrue(reserved.reservation.is_file())
            with self.assertRaisesRegex(ProjectWorktreeError, "reserved"):
                reserve_project_worktree(
                    self.root,
                    purpose="Release 1.2 integration",
                    branch_or_revision="release/1.2",
                )
        self.assertFalse(reserved.reservation.exists())

    def test_existing_target_and_unsafe_root_are_refused(self) -> None:
        first = reserve_project_worktree(
            self.root, purpose="audit", branch_or_revision="refs/heads/audit"
        )
        first.release()
        first.path.mkdir()
        with self.assertRaisesRegex(ProjectWorktreeError, "collides"):
            reserve_project_worktree(
                self.root, purpose="audit", branch_or_revision="refs/heads/audit"
            )

        if os.name != "nt":
            first.path.rmdir()
            (self.root / ".worktrees" / ".reservations").rmdir()
            (self.root / ".worktrees").rmdir()
            outside = Path(self.temporary.name) / "outside"
            outside.mkdir()
            (self.root / ".worktrees").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ProjectWorktreeError, "unsafe"):
                resolve_project_worktree_location(self.root)

    def test_bare_repository_is_refused(self) -> None:
        bare = Path(self.temporary.name) / "bare.git"
        subprocess.run(
            ("git", "init", "--bare", "-q", str(bare)),
            check=True,
            capture_output=True,
        )
        with self.assertRaises(ProjectWorktreeError):
            resolve_project_worktree_location(bare)

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
