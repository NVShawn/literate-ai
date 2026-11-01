"""Recorded excerpt caps do not become retained command acceptance gates."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.harness_inventory import (
    HARNESS_DIAGNOSTIC_CHARS,
    MAX_HARNESS_OUTPUT_BYTES,
    HarnessBaselineError,
    execute_retained_harness,
)


class RetainedOutputDiagnosticsTests(unittest.TestCase):
    def run_retained(self, root: Path, *, fail: bool):
        emitted = b"x" * (MAX_HARNESS_OUTPUT_BYTES + 65536) + b"\n"
        (root / "test_emit.py").write_text(
            "import sys, unittest\n"
            f"sys.stdout.buffer.write(b'x' * {len(emitted) - 1} + b'\\n')\n"
            "class Cases(unittest.TestCase):\n"
            "    def test_one(self): self.assertEqual(2 + 2, 4)\n"
            "    def test_two(self): self.assertIsInstance({}, dict)\n"
            f"    def test_three(self): self.assertTrue({not fail!r})\n",
            encoding="utf-8",
        )
        argv = [sys.executable, "-m", "unittest", "test_emit"]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        inventory = {
            "commands": {"test": {"command": command, "evidence": "test_emit.py"}},
            "findings": [],
        }
        report = execute_retained_harness(
            inventory, legacy_root=root, timeout_seconds=30
        )
        phase = report["phases"][0]
        self.assertEqual(
            phase["stdout_identity"], "sha256:" + hashlib.sha256(emitted).hexdigest()
        )
        return report

    def assert_excerpt_is_bounded(self, phase):
        self.assertFalse(phase["output_within_limits"])
        self.assertGreater(phase["stdout_size"], phase["stdout_limit_bytes"])
        self.assertLessEqual(
            len(phase["stdout_excerpt"]) + len(phase["stderr_excerpt"]),
            HARNESS_DIAGNOSTIC_CHARS,
        )

    def test_successful_real_tests_are_admitted_above_recorded_stdout_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_retained(Path(directory), fail=False)
        self.assertEqual(report["state"], "passed")
        phase = report["phases"][0]
        self.assertEqual(phase["exit_code"], 0)
        self.assertEqual(phase["test_collection"]["passed"], 3)
        self.assertEqual(phase["test_collection"]["failed"], 0)
        self.assert_excerpt_is_bounded(phase)

    def test_failure_names_exit_status_and_capped_excerpts_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessBaselineError) as caught:
                self.run_retained(Path(directory), fail=True)
        phase = caught.exception.report["phases"][0]
        self.assertNotEqual(phase["exit_code"], 0)
        self.assertIn("failed with status 1", caught.exception.message)
        self.assertIn("recorded-output excerpts were capped", caught.exception.message)
        self.assertNotIn("exceeded its bounded output policy", caught.exception.message)
        self.assert_excerpt_is_bounded(phase)
