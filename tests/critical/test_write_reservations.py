"""Actual exclusive markers, exact ownership, and non-destructive release."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_competing_owner_fails_without_disturbing_first_owner(self):
        with self.acquire("index.lock") as first:
            content = (self.root / "index.lock").read_bytes()
            with self.assertRaises(OrchestrationInventoryError):
                with self.acquire("index.lock"):
                    self.fail("second owner")
            first.verify_all()
            self.assertEqual((self.root / "index.lock").read_bytes(), content)
        self.assertEqual(list(self.root.iterdir()), [])

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
