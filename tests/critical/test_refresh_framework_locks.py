"""Framework writer interoperability of acknowledged multi-root reservations."""

from __future__ import annotations

import multiprocessing
import os
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_reservations as reservations
from literate_ai.adapters.lifecycle_lock import (
    ProjectLifecycleLockError,
    project_lifecycle_lock,
    project_lifecycle_lock_path,
)
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.projects import ProjectConfigurationStore
from tests.support import fixtures_test_repository_refresh_inputs as fixtures
from tests.support.fixtures_test_repository_orchestration import snapshot


def _lock_metadata(path):
    node = path.stat()
    return (
        node.st_dev,
        node.st_ino,
        node.st_mode,
        node.st_size,
        node.st_mtime_ns,
        node.st_nlink,
    )


def _manifest_writer(root, attempting, entered, release):
    original = ProjectConfigurationStore._acquire_exclusive

    def acquire(descriptor):
        attempting.set()
        original(descriptor)

    with patch.object(
        ProjectConfigurationStore, "_acquire_exclusive", staticmethod(acquire)
    ):
        with ProjectConfigurationStore(Path(root))._lock(Path(root)):
            entered.set()
            if not release.wait(60):
                raise RuntimeError("fixture release deadline")


class RefreshFrameworkLockTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RepositoryRefreshInputTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base = fixture.root, fixture.base

    def acquire(self, prepared, **options):
        return reservations.reserve_refresh_inputs(
            prepared,
            expected_custody_identity=options.pop(
                "expected_custody_identity",
                reservations.refresh_custody_identity(prepared),
            ),
            acknowledge=options.pop("acknowledge", True),
            **options,
        )

    def test_lifecycle_writers_on_every_observed_root_are_excluded(self):
        prepared = self.fixture.prepare()
        before = snapshot(self.base)
        with self.acquire(prepared):
            for observed in (prepared.root_git, *prepared.children):
                with self.assertRaises(ProjectLifecycleLockError):
                    with project_lifecycle_lock(observed.root, operation="fixture"):
                        self.fail("lifecycle writer entered")
        self.assertEqual(snapshot(self.base), before)

    def test_existing_lifecycle_holder_prevents_reservation_and_is_preserved(self):
        prepared = self.fixture.prepare()
        # Establish the idle placeholder before the complete byte snapshot.
        # Windows forbids reading its locked byte through another descriptor.
        with project_lifecycle_lock(self.root, operation="fixture"):
            pass
        before = snapshot(self.base)
        lock = project_lifecycle_lock_path(self.root)
        with project_lifecycle_lock(self.root, operation="fixture"):
            held = _lock_metadata(lock)
            with self.assertRaises(OrchestrationInventoryError):
                with self.acquire(prepared):
                    self.fail("live holder")
            self.assertEqual(_lock_metadata(lock), held)
            with self.assertRaises(ProjectLifecycleLockError):
                with project_lifecycle_lock(self.root, operation="contender"):
                    self.fail("original holder lost its lock")
        self.assertEqual(snapshot(self.base), before)

    def test_manifest_writer_process_waits_until_reservation_is_released(self):
        self._check_manifest_writer_handoff(existing=True)

    def _check_manifest_writer_handoff(self, *, existing):
        marker = self.root / ".literate.project.json.write.lock"
        if existing:
            marker.write_bytes(b"\0")
        prepared = self.fixture.prepare()
        context = multiprocessing.get_context("spawn")
        attempting, entered, release = (context.Event() for _ in range(3))
        process = context.Process(
            target=_manifest_writer, args=(str(self.root), attempting, entered, release)
        )
        # Windows cannot unlink a new marker while the waiter has it open.
        # Retention is a required typed failure, never successful cleanup.
        retained = os.name == "nt" and not existing
        try:
            with (
                self.assertRaises(OrchestrationInventoryError)
                if retained
                else nullcontext()
            ) as caught:
                with self.acquire(prepared):
                    node = _lock_metadata(marker)
                    process.start()
                    self.assertTrue(attempting.wait(30), "writer did not reach OS lock")
                    self.assertFalse(
                        entered.wait(0.2), "writer bypassed held reservation"
                    )
            if retained:
                self.assertEqual(
                    caught.exception.code,
                    "orchestration.reservation_cleanup_incomplete",
                )
                self.assertEqual(_lock_metadata(marker), node)
            self.assertTrue(
                entered.wait(30), "writer did not acquire the released lock"
            )
            release.set()
            process.join(30)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(entered.is_set())
        finally:
            release.set()
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(5)
