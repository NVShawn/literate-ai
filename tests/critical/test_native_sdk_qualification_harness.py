"""Keep native qualification failure diagnostics through child failure/timeout."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class NativeSdkQualificationHarnessTests(unittest.TestCase):
    @staticmethod
    def harness():
        path = Path(__file__).resolve().parents[2] / (
            ".github/qualifications/native_sdk/run_cell.py"
        )
        spec = importlib.util.spec_from_file_location("native_sdk_cell", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_failed_real_child_retains_both_streams_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "exit 17"):
                self.harness().command(
                    "failure",
                    [
                        sys.executable,
                        "-c",
                        "import os;os.write(1,b'out');os.write(2,b'err');"
                        "raise SystemExit(17)",
                    ],
                    output,
                    10,
                )
            self.assertEqual((output / "failure.stdout").read_bytes(), b"out")
            self.assertEqual((output / "failure.stderr").read_bytes(), b"err")
            terminal = json.loads((output / "failure.terminal.json").read_bytes())
            self.assertEqual(terminal["exit_code"], 17)
            self.assertFalse(terminal["timed_out"])

    def test_timed_out_real_child_retains_partial_streams_and_failed_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaises(subprocess.TimeoutExpired):
                self.harness().command(
                    "timeout",
                    [
                        sys.executable,
                        "-c",
                        "import os,time;os.write(1,b'partial-out');"
                        "os.write(2,b'partial-err');time.sleep(30)",
                    ],
                    output,
                    3,
                )
            self.assertEqual((output / "timeout.stdout").read_bytes(), b"partial-out")
            self.assertEqual((output / "timeout.stderr").read_bytes(), b"partial-err")
            terminal = json.loads((output / "timeout.terminal.json").read_bytes())
            self.assertIsNone(terminal["exit_code"])
            self.assertTrue(terminal["timed_out"])
