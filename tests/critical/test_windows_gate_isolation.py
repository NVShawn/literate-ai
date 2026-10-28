"""Native gate keeps the live dependency observation outside xdist contention."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WindowsGateIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[2] / "scripts/windows_release_gate.py"
        spec = importlib.util.spec_from_file_location("windows_gate_fixture", source)
        cls.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gate)

    def test_live_case_runs_serially_before_remaining_parallel_roots(self):
        with patch.object(self.gate, "_run") as run:
            self.gate._run_tests(Path("python"), "4", {})
        self.assertEqual(run.call_count, 2)
        serial, parallel = (call.args[0] for call in run.call_args_list)
        self.assertIn(self.gate._ADMITTED_RECEIVER_TEST, serial)
        self.assertNotIn("-n", serial)
        self.assertEqual(serial[serial.index("-o") + 1], "addopts=")
        self.assertEqual(parallel[parallel.index("-n") + 1], "4")
        self.assertIn(f"--deselect={self.gate._ADMITTED_RECEIVER_TEST}", parallel)
        self.assertNotIn(self.gate._ADMITTED_RECEIVER_TEST, parallel)
        self.assertEqual(tuple(parallel[-4:]), self.gate._TEST_ROOTS)
        self.assertNotEqual(
            next(arg for arg in serial if arg.startswith("--junitxml=")),
            next(arg for arg in parallel if arg.startswith("--junitxml=")),
        )

    def test_single_worker_request_keeps_the_remaining_suite_serial(self):
        with patch.object(self.gate, "_run") as run:
            self.gate._run_tests(Path("python"), "1", {})
        parallel = run.call_args_list[-1].args[0]
        self.assertEqual(parallel[parallel.index("-n") + 1], "0")

    def test_isolated_failure_prevents_parallel_execution(self):
        with patch.object(
            self.gate, "_run", side_effect=self.gate.GateFailure("isolated failure")
        ) as run:
            with self.assertRaisesRegex(self.gate.GateFailure, "isolated failure"):
                self.gate._run_tests(Path("python"), "4", {})
        self.assertEqual(run.call_count, 1)

    def test_parallel_failure_is_not_reported_as_success(self):
        with patch.object(
            self.gate,
            "_run",
            side_effect=[None, self.gate.GateFailure("parallel failure")],
        ) as run:
            with self.assertRaisesRegex(self.gate.GateFailure, "parallel failure"):
                self.gate._run_tests(Path("python"), "4", {})
        self.assertEqual(run.call_count, 2)

    def test_native_run_directories_preserve_failed_and_legacy_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "checkout"
            root.mkdir()
            legacy = parent / "checkout.tmp"
            legacy.mkdir()
            (legacy / "original").write_bytes(b"original failed gate evidence")
            runs = []
            original_run = self.gate._run

            def execute(argv, environment, *, step):
                temporary = Path(environment["TEMP"])
                self.assertEqual(environment["TMP"], str(temporary))
                self.assertIn(
                    str(temporary),
                    environment["GIT_CEILING_DIRECTORIES"].split(self.gate.os.pathsep),
                )
                self.assertEqual(temporary.parent, parent)
                self.assertNotEqual(temporary, legacy)
                self.assertIn(
                    f"--junitxml={temporary / 'windows-admitted-receiver.xml'}", argv
                )
                runs.append(temporary)
                # Exercise a real failing child and retain its partial output.
                original_run(
                    [
                        sys.executable,
                        "-c",
                        "import os; from pathlib import Path; "
                        "Path(os.environ['TEMP'], 'partial').write_bytes(b'failed'); "
                        "raise SystemExit(7)",
                    ],
                    environment,
                    step=step,
                )

            with (
                patch.object(self.gate, "ROOT", root),
                patch.object(self.gate, "TEMPORARY", legacy),
                patch.object(self.gate, "_windows", return_value=True),
                patch.object(self.gate, "_template_commands", return_value=[]),
                patch.object(
                    self.gate, "_environment_python", return_value=Path(sys.executable)
                ),
                patch.object(self.gate, "_run", side_effect=execute),
            ):
                for _ in range(2):
                    self.assertEqual(self.gate.main(["--only", "tests"]), 1)
            self.assertEqual(len(runs), 2)
            self.assertNotEqual(runs[0], runs[1])
            for run in runs:
                self.assertEqual((run / "partial").read_bytes(), b"failed")
            self.assertEqual(
                (legacy / "original").read_bytes(), b"original failed gate evidence"
            )

    def test_run_specific_reports_and_pytest_base_preserve_test_inventory(self):
        temporary = Path("owned-run")
        with patch.object(self.gate, "_run") as run:
            self.gate._run_tests(Path("python"), "4", {}, temporary=temporary)
        serial, parallel = (call.args[0] for call in run.call_args_list)
        self.assertIn(
            f"--junitxml={temporary / 'windows-admitted-receiver.xml'}", serial
        )
        self.assertIn(f"--junitxml={temporary / 'windows-tests.xml'}", parallel)
        self.assertIn(f"--basetemp={temporary / 'pytest'}", parallel)
        self.assertEqual(tuple(parallel[-4:]), self.gate._TEST_ROOTS)
        self.assertIn(f"--deselect={self.gate._ADMITTED_RECEIVER_TEST}", parallel)
