from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_write_reservations``."""

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from literate_ai.adapters import _write_reservations as reservations
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError


class WriteReservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="write-res-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        node = self.root.stat()
        self.node = (node.st_dev, node.st_ino, node.st_mode)

    def target(self, name):
        return reservations.WriteReservationTarget(
            self.root / name, self.root, self.node
        )

    def acquire(self, *names):
        return reservations.acquire_write_reservations(
            tuple(self.target(name) for name in names)
        )

    def test_owned_markers_are_ordered_deduplicated_and_removed_on_success(self):
        opened = []
        original = reservations.os.open

        def record(path, flags, *args, **kwargs):
            if flags & os.O_EXCL:
                opened.append(Path(path).name)
            return original(path, flags, *args, **kwargs)

        with patch.object(reservations.os, "open", record):
            with self.acquire("z.lock", "a.lock", "a.lock") as owned:
                self.assertEqual(opened, ["a.lock", "z.lock"])
                owned.verify_all()
                self.assertTrue(owned.owns(self.root / "a.lock"))
                self.assertFalse(owned.owns(self.root / "other.lock"))
                first = (self.root / "a.lock").read_bytes()
                self.assertTrue(first.startswith(b"literate-ai-refresh-reservation\n"))
                self.assertNotEqual(first, (self.root / "z.lock").read_bytes())
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(OrchestrationInventoryError):
            owned.verify_all()

    def test_partial_acquisition_preserves_preexisting_marker(self):
        foreign = self.root / "z.lock"
        foreign.write_bytes(b"other writer")
        before = foreign.stat().st_mtime_ns
        with self.assertRaises(OrchestrationInventoryError) as caught:
            with self.acquire("a.lock", "z.lock"):
                self.fail("contended acquisition must not yield")
        self.assertEqual(caught.exception.code, "orchestration.reservation_busy")
        self.assertEqual(list(self.root.iterdir()), [foreign])
        self.assertEqual(foreign.read_bytes(), b"other writer")
        self.assertEqual(foreign.stat().st_mtime_ns, before)

    def test_competing_owner_fails_without_disturbing_first_owner(self):
        with self.acquire("index.lock") as first:
            content = (self.root / "index.lock").read_bytes()
            with self.assertRaises(OrchestrationInventoryError):
                with self.acquire("index.lock"):
                    self.fail("second owner")
            first.verify_all()
            self.assertEqual((self.root / "index.lock").read_bytes(), content)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_exception_removes_only_matching_owned_files(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with self.acquire("refs/heads/topic.lock"):
                raise RuntimeError("fixture failure")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_changed_marker_content_is_retained(self):
        path = self.root / "index.lock"
        with self.assertRaises(OrchestrationInventoryError) as caught:
            with self.acquire("index.lock") as owned:
                path.write_bytes(b"foreign replacement")
                with self.assertRaises(OrchestrationInventoryError):
                    owned.verify_all()
        self.assertEqual(
            caught.exception.code, "orchestration.reservation_cleanup_incomplete"
        )
        self.assertEqual(path.read_bytes(), b"foreign replacement")

    @unittest.skipIf(os.name == "nt", "Windows does not rename an open CRT file")
    def test_same_byte_replacement_is_not_the_owned_descriptor(self):
        path = self.root / "index.lock"
        moved = self.root / "moved.lock"
        with self.assertRaises(OrchestrationInventoryError):
            with self.acquire("index.lock"):
                content = path.read_bytes()
                path.rename(moved)
                path.write_bytes(content)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(moved.read_bytes(), content)

    def test_foreign_entries_keep_new_parent_directories_intact(self):
        foreign = self.root / "refs/heads/foreign"
        with self.assertRaises(OrchestrationInventoryError) as caught:
            with self.acquire("refs/heads/topic.lock"):
                foreign.write_bytes(b"do not delete")
        self.assertEqual(
            caught.exception.code, "orchestration.reservation_cleanup_incomplete"
        )
        self.assertEqual(foreign.read_bytes(), b"do not delete")
        self.assertFalse((self.root / "refs/heads/topic.lock").exists())

    def test_existing_parent_directories_are_not_removed(self):
        parent = self.root / "refs/heads"
        parent.mkdir(parents=True)
        with self.acquire("refs/heads/topic.lock"):
            pass
        self.assertTrue(parent.is_dir())

    def test_linked_parent_is_refused_without_touching_its_target(self):
        destination = self.root / "foreign"
        destination.mkdir()
        sentinel = destination / "topic.lock"
        sentinel.write_bytes(b"foreign owner")
        try:
            (self.root / "refs").symlink_to(destination, target_is_directory=True)
        except OSError:
            self.skipTest("host cannot create directory symlinks")
        with self.assertRaises(OrchestrationInventoryError):
            with self.acquire("refs/topic.lock"):
                self.fail("linked parent")
        self.assertEqual(sentinel.read_bytes(), b"foreign owner")
        self.assertTrue((self.root / "refs").is_symlink())

    def test_portable_aliases_fail_before_marker_creation(self):
        (self.root / "INDEX.lock").write_bytes(b"foreign")
        with self.assertRaises(OrchestrationInventoryError):
            with self.acquire("index.lock"):
                self.fail("alias")
        self.assertEqual((self.root / "INDEX.lock").read_bytes(), b"foreign")
        with self.assertRaises(OrchestrationInventoryError):
            with self.acquire("a.lock", "A.lock"):
                self.fail("target aliases")
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_windows_path_aliases_refuse_before_any_acquisition(self):
        anchor = PureWindowsPath("C:/fixture")
        first = reservations.WriteReservationTarget(
            anchor / "refs/a.lock", anchor, (1, 2, 3)
        )
        for changed in (
            replace(first, path=anchor / "refs/A.lock"),
            replace(first, path=anchor / "REFS/a.lock"),
            replace(first, anchor=PureWindowsPath("C:/FIXTURE")),
        ):
            with (
                self.subTest(changed=changed),
                patch.object(reservations.WriteReservationSet, "_acquire") as acquire,
            ):
                with self.assertRaises(OrchestrationInventoryError) as caught:
                    with reservations.acquire_write_reservations((first, changed)):
                        self.fail("Windows aliases must not yield ownership")
                self.assertEqual(
                    caught.exception.code, "orchestration.reservation_alias"
                )
                acquire.assert_not_called()

    def test_partial_token_write_failure_cleans_the_owned_prefix(self):
        original = reservations.os.write
        calls = 0

        def partial(descriptor, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original(descriptor, data[:3])
            raise OSError("fixture write failure")

        with patch.object(reservations.os, "write", partial):
            with self.assertRaises(OrchestrationInventoryError):
                with self.acquire("index.lock"):
                    self.fail("incomplete token")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_readable_documents_cannot_construct_live_ownership(self):
        with self.assertRaises(TypeError):
            reservations.WriteReservationSet({"owned": True})

    def run_other_process(self, *, crash=False):
        script = """
import os, sys
from pathlib import Path
from literate_ai.adapters._write_reservations import (
    WriteReservationTarget, acquire_write_reservations,
)
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
root = Path(sys.argv[1])
node = root.stat()
target = WriteReservationTarget(root / 'index.lock', root,
                               (node.st_dev, node.st_ino, node.st_mode))
try:
    with acquire_write_reservations((target,)):
        if sys.argv[2] == 'crash':
            os._exit(23)
except OrchestrationInventoryError as error:
    print(error.code)
    sys.exit(24)
"""
        return subprocess.run(
            [sys.executable, "-c", script, str(self.root), "crash" if crash else "try"],
            capture_output=True,
            text=True,
            timeout=20,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(reservations.__file__).resolve().parents[2]),
            },
        )

    def test_separate_process_cannot_steal_live_reservation(self):
        with self.acquire("index.lock") as owned:
            before = (self.root / "index.lock").read_bytes()
            result = self.run_other_process()
            self.assertEqual(result.returncode, 24, result.stderr)
            self.assertIn("orchestration.reservation_busy", result.stdout)
            owned.verify_all()
            self.assertEqual((self.root / "index.lock").read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_crashed_owner_marker_remains_an_explicit_blocker(self):
        result = self.run_other_process(crash=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        path = self.root / "index.lock"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            with self.acquire("index.lock"):
                self.fail("crashed marker must not be reclaimed")
        self.assertEqual(caught.exception.code, "orchestration.reservation_busy")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def advisory(self, name):
        return reservations.acquire_write_reservations(
            (replace(self.target(name), advisory=True),)
        )

    def test_existing_advisory_bytes_and_timestamp_survive_success_and_failure(self):
        path = self.root / "advisory.lock"
        path.write_bytes(b"\0")
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with self.advisory(path.name) as owned:
            owned.verify_all()
        with self.assertRaisesRegex(RuntimeError, "body"):
            with self.advisory(path.name):
                raise RuntimeError("body")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_empty_retained_advisory_file_is_not_rewritten(self):
        path = self.root / "advisory.lock"
        path.write_bytes(b"")
        before = path.stat().st_mtime_ns
        with self.assertRaises(OrchestrationInventoryError) as caught:
            with self.advisory(path.name):
                self.fail("empty retained lock")
        self.assertEqual(caught.exception.code, "orchestration.reservation_empty")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (b"", before))

    def test_failed_advisory_acquisition_does_not_unlink_possible_peer_lock(self):
        path = self.root / "advisory.lock"
        with patch.object(
            reservations, "_acquire", side_effect=reservations.CacheLockError("busy")
        ):
            with self.assertRaises(OrchestrationInventoryError):
                with self.advisory(path.name):
                    self.fail("no advisory ownership")
        self.assertTrue(
            path.read_bytes().startswith(b"literate-ai-refresh-reservation\n")
        )

    def test_retained_refresh_token_cannot_be_reclaimed_as_idle_advisory_file(self):
        result = self.run_other_process(crash=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        path = self.root / "index.lock"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            with self.advisory(path.name):
                self.fail("crashed reservation")
        self.assertEqual(caught.exception.code, "orchestration.reservation_busy")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_changed_existing_advisory_file_is_retained(self):
        path = self.root / "advisory.lock"
        path.write_bytes(b"\0")
        with self.assertRaises(OrchestrationInventoryError):
            with self.advisory(path.name) as owned:
                # The held descriptor may write its own locked byte on Windows.
                marker = owned._markers[0]
                os.lseek(marker.descriptor, 0, os.SEEK_SET)
                os.write(marker.descriptor, b"foreign")
                with self.assertRaises(OrchestrationInventoryError):
                    owned.verify_all()
        self.assertEqual(path.read_bytes(), b"foreign")
