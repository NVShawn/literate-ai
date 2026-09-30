"""scripts/profile_pytest.py wraps pytest with a structured trace (#57)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import profile_pytest  # noqa: E402


@unittest.skipUnless(
    importlib.util.find_spec("pytest"),
    "pytest is an optional contributor dependency",
)
class ProfilePytestScriptTests(unittest.TestCase):
    def test_bytecode_suppression_is_inherited_and_restored(self) -> None:
        previous_runtime = sys.dont_write_bytecode
        with mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "0"}):
            with profile_pytest._suppress_source_tree_bytecode():
                self.assertTrue(sys.dont_write_bytecode)
                self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "0")
        self.assertEqual(sys.dont_write_bytecode, previous_runtime)

    def test_propagates_the_pytest_exit_code_and_writes_a_typed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            passing = root / "test_ok.py"
            passing.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
            log_path = root / "profile.ndjson"
            report_path = root / "profile-report.json"

            exit_code = profile_pytest.main(
                [
                    str(log_path),
                    str(report_path),
                    "--",
                    str(passing),
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["schema"], "literate-ai/profile-report@1")
            self.assertEqual(report["command_result"], {"exit_code": 0})

    def test_a_failing_run_returns_a_nonzero_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failing = root / "test_bad.py"
            failing.write_text("def test_bad():\n    assert False\n", encoding="utf-8")
            log_path = root / "profile.ndjson"
            report_path = root / "profile-report.json"

            exit_code = profile_pytest.main(
                [
                    str(log_path),
                    str(report_path),
                    "--",
                    str(failing),
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ]
            )

            self.assertNotEqual(exit_code, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertNotEqual(report["command_result"]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
