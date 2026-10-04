"""Real-Git refresh custody without child commands, publication or mutation."""

from __future__ import annotations

import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_refresh as refresh
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.support import fixtures_test_repository_lock_planning as lock_fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class RepositoryRefreshInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build the two-child superproject once; each test gets a private copy.
        fixture = lock_fixtures.RepositoryLockPlanningTests()
        fixture.setUp()
        cls.addClassCleanup(fixture.doCleanups)
        for name in ("app", "lib"):
            child = fixture.root / name
            git(
                fixture.base,
                "clone",
                "-q",
                "--no-hardlinks",
                str(fixture.root),
                str(child),
            )
            git(child, "checkout", "-q", fixture.fixture.pin)
            git(child, "config", "user.name", "Test")
            git(child, "config", "user.email", "test@example.test")
        cls.template = fixture.base

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve() / "base"
        shutil.copytree(self.template, self.base, symlinks=True)
        self.root = self.base / "super"
        for name in ("app", "lib"):
            child = self.root / name
            git(child, "config", "remote.origin.url", str(self.root))
            git(child, "update-index", "-q", "--refresh")
        git(self.root, "update-index", "-q", "--refresh")
        self.request = RepositoryRefreshRequest(
            (RepositoryRefreshTarget("app", "4" * 40),)
        )

    def prepare(self):
        return refresh.prepare_repository_refresh(self.root, self.request)

    def assert_refuses_without_writes(self, suffix):
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "orchestration." + suffix)
        self.assertEqual(snapshot(self.base), before)

    def test_dirty_tracked_staged_untracked_and_deleted_child_files_refuse(self):
        source = self.root / "app/source.txt"
        original = source.read_bytes()
        for kind in ("tracked", "staged", "untracked", "deleted"):
            with self.subTest(kind=kind):
                if kind == "untracked":
                    (source.parent / "extra.txt").write_bytes(b"untracked")
                elif kind == "deleted":
                    source.unlink()
                else:
                    source.write_bytes(b"changed\n")
                    if kind == "staged":
                        git(source.parent, "add", "source.txt")
                self.assert_refuses_without_writes("refresh_child_dirty")
                if kind == "untracked":
                    (source.parent / "extra.txt").unlink()
                source.write_bytes(original)
                git(source.parent, "add", "source.txt")

    def test_real_child_clean_filter_cannot_execute_during_inspection(self):
        child = self.root / "app"
        (child / ".gitattributes").write_bytes(b"source.txt filter=fixture\n")
        (child / "filter.py").write_bytes(
            b"import pathlib, sys\n"
            b"pathlib.Path('filter-ran').write_bytes(b'observed')\n"
            b"sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        )
        git(child, "add", ".gitattributes", "filter.py")
        git(child, "commit", "-q", "-m", "filter fixture")
        git(
            child,
            "config",
            "filter.fixture.clean",
            shlex.quote(sys.executable) + " filter.py",
        )
        (child / "source.txt").write_bytes(b"changed fixture\n")
        self.assert_refuses_without_writes("refresh_filter_unsupported")
        self.assertFalse((child / "filter-ran").exists())

    def test_in_progress_git_operations_refuse_even_with_clean_source(self):
        for name in ("HEAD.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge"):
            with self.subTest(name=name):
                marker = self.root / "app/.git" / name
                if name == "rebase-merge":
                    marker.mkdir()
                else:
                    marker.write_bytes(b"foreign Git operation")
                self.assert_refuses_without_writes("refresh_busy")
                if marker.is_dir():
                    marker.rmdir()
                else:
                    marker.unlink()

    def test_reobservation_rejects_late_foreign_changes_without_repairing_them(self):
        observe = refresh._observe_checked
        calls = 0
        foreign = self.root / "app/foreign.txt"

        def racing(root, request, reservations=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                foreign.write_bytes(b"do not undo")
            return observe(root, request, reservations)

        with patch.object(refresh, "_observe_checked", side_effect=racing):
            with self.assertRaises(OrchestrationInventoryError):
                self.prepare()
        self.assertEqual(foreign.read_bytes(), b"do not undo")
