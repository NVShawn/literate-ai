"""Immutable cache installation never replaces a concurrent owner's data."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_object_install as install
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from tests.support import fixtures_test_refresh_file_custody as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class ObjectFileInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "object.pack"

    def test_collision_preserves_existing_foreign_bytes(self):
        self.path.write_bytes(b"foreign")
        before = snapshot(self.root)
        with self.assertRaises(OrchestrationInventoryError):
            install._install_file(self.path, b"verified", lambda: None)
        self.assertEqual(snapshot(self.root), before)

    def test_concurrent_publication_winner_is_never_overwritten(self):
        link = install.os.link

        def winner(source, destination, **options):
            Path(destination).write_bytes(b"foreign winner")
            return link(source, destination, **options)

        with patch.object(install.os, "link", winner):
            with self.assertRaises(OrchestrationInventoryError):
                install._install_file(self.path, b"verified", lambda: None)
        self.assertEqual(self.path.read_bytes(), b"foreign winner")
        self.assertEqual(list(self.root.iterdir()), [self.path])


class RefreshObjectInstallationTests(unittest.TestCase):
    def test_live_install_adds_missing_objects_without_changing_files_refs_or_pins(
        self,
    ):
        fixture = fixtures.RefreshFileCustodyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.fixture
        upstream = source.base / "upstream"
        git(
            source.base,
            "clone",
            "--no-hardlinks",
            source.remote.as_uri(),
            str(upstream),
        )
        git(upstream, "config", "user.name", "Test")
        git(upstream, "config", "user.email", "test@example.test")
        (upstream / "source.txt").write_bytes(b"new published content\n")
        git(upstream, "add", "source.txt")
        git(upstream, "commit", "-q", "-m", "remote-only change")
        target = git(upstream, "rev-parse", "HEAD").decode().strip()
        git(upstream, "push", source.remote.as_uri(), "HEAD:refs/heads/main")
        with self.assertRaises(subprocess.CalledProcessError):
            git(source.child, "cat-file", "-e", target)
        git(source.child, "checkout", "-q", "-b", "refresh-target")
        git(source.child, "pack-refs", "--all", "--prune")
        prepared = source.prepare(source.inputs(target))
        before = snapshot(source.base)
        with fixture.acquire(prepared) as owner:
            files = owner.prepare_filesystem_changes()
            objects = files.prepare_objects()
            with objects.stage(metadata=True) as stage:
                reference = next(
                    item
                    for item in stage.metadata
                    if item.kind == "ref" and item.repository == "app"
                )
                self.assertIsNone(reference.before.content)
                self.assertEqual(reference.prospective, (target + "\n").encode())
                self.assertFalse(reference.before.path.exists())
                with stage.installed_objects() as installed:
                    installed.require_current()
                    record = installed.records[0]
                    prefix = record.directory / ("pack-" + record.objects.pack_id)
                    self.assertTrue(prefix.with_suffix(".keep").is_file())
                    self.assertEqual(
                        git(source.child, "show", target + ":source.txt"),
                        b"new published content\n",
                    )
                    self.assertEqual(
                        git(source.child, "rev-parse", "HEAD").decode().strip(),
                        source.pin,
                    )
                    self.assertFalse(reference.before.path.exists())
                self.assertFalse(prefix.with_suffix(".keep").exists())
                with self.assertRaises(OrchestrationInventoryError):
                    installed.require_current()
        # Recent Git also verifies ref filenames/content during fsck and reports
        # our live refs/heads/*.lock token as badRefContent. Require the full
        # integrity check after releasing markers, without disabling ref checks.
        git(source.child, "fsck", "--full", "--no-reflogs", target)
        after = snapshot(source.base)
        for suffix, content in (
            (".pack", record.objects.pack),
            (".idx", record.objects.index),
        ):
            path = prefix.with_suffix(suffix)
            self.assertEqual(
                after.pop(path.relative_to(source.base).as_posix())[0], content
            )
        self.assertEqual(after, before)
