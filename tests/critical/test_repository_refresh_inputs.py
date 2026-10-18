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

    def test_configuration_custody_retains_digest_without_credential_bytes(self):
        secret = "fixture-private-header-782435"
        git(self.root / "app", "config", "http.extraheader", "Authorization: " + secret)
        prepared = self.prepare()
        self.assertNotIn(secret, repr(prepared))
        refresh.require_repository_refresh_inputs_unchanged(prepared)

    def test_packed_commit_fallback_rechecks_real_git_object_availability(self):
        child = self.root / "app"
        git(child, "gc", "--prune=now")
        prepared = self.prepare()
        observed = next(item for item in prepared.children if item.root == child)
        self.assertIsNone(observed.loose_commit)
        refresh.require_repository_refresh_inputs_unchanged(prepared)
        indexes = list((child / ".git/objects/pack").glob("*.idx"))
        self.assertTrue(indexes)
        indexes[0].chmod(0o600)
        indexes[0].write_bytes(b"corrupt packed index")
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(prepared)
        self.assertEqual(snapshot(self.base), before)

    def test_retained_revalidation_rejects_metadata_and_registration_drift(self):
        for kind in (
            "index",
            "head",
            "modules",
            "gitlink",
            "registration",
            "common-locator",
            "configuration",
            "included-configuration",
            "loose-commit",
            "reference",
            "packed-reference",
            "manifest",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve() / "copy"
                shutil.copytree(self.base, base, symlinks=True)
                root = base / "super"
                child = root / "app"
                if kind == "packed-reference":
                    git(root, "pack-refs", "--all", "--prune")
                include = child / ".git" / "refresh-include.config"
                if kind == "included-configuration":
                    include.write_text("[user]\n name = Before\n")
                    git(child, "config", "include.path", str(include))
                prepared = refresh.prepare_repository_refresh(root, self.request)
                if kind == "index":
                    git(child, "update-index", "--assume-unchanged", "source.txt")
                elif kind == "head":
                    (child / ".git/HEAD").write_bytes(b"f" * 40 + b"\n")
                elif kind == "modules":
                    with (root / ".gitmodules").open("ab") as stream:
                        stream.write(b"\n# changed after preparation\n")
                elif kind == "gitlink":
                    git(
                        root,
                        "update-index",
                        "--cacheinfo",
                        "160000," + "f" * 40 + ",app",
                    )
                elif kind == "registration":
                    git(
                        child, "worktree", "add", "--detach", str(base / "peer"), "HEAD"
                    )
                elif kind == "reference":
                    prepared.root_git.references[0].file.path.write_bytes(
                        b"f" * 40 + b"\n"
                    )
                elif kind == "packed-reference":
                    path = prepared.root_git.packed_references.path
                    path.write_bytes(
                        path.read_bytes() + b"# changed after preparation\n"
                    )
                elif kind == "manifest":
                    (root / "literate.project.json").write_bytes(b'{"changed":true}')
                elif kind == "configuration":
                    git(child, "config", "user.name", "After")
                elif kind == "included-configuration":
                    include.write_text("[user]\n name = After\n")
                elif kind == "loose-commit":
                    observed = next(
                        item for item in prepared.children if item.root == child
                    )
                    self.assertIsNotNone(observed.loose_commit)
                    observed.loose_commit.path.write_bytes(b"corrupt object")
                else:
                    (child / ".git/commondir").write_text(str(root / ".git") + "\n")
                before = snapshot(base)
                with self.assertRaises(OrchestrationInventoryError):
                    refresh.require_repository_refresh_inputs_unchanged(prepared)
                self.assertEqual(snapshot(base), before)

    def test_reobservation_rejects_late_foreign_changes_without_repairing_them(self):
        observe = refresh._observe_checked
        calls = 0
        foreign = self.root / "app/foreign.txt"

        def racing(root, request, reservations=None, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                foreign.write_bytes(b"do not undo")
            return observe(root, request, reservations, **kwargs)

        with patch.object(refresh, "_observe_checked", side_effect=racing):
            with self.assertRaises(OrchestrationInventoryError):
                self.prepare()
        self.assertEqual(foreign.read_bytes(), b"do not undo")
