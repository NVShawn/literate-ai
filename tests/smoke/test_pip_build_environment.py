"""Dependency build subprocesses must not inherit the outer Make jobserver."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PipBuildEnvironmentTests(unittest.TestCase):
    def test_pip_receives_arguments_and_policy_without_make_control(self) -> None:
        wrapper = Path(__file__).resolve().parents[2] / "scripts" / "run_pip.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Observe the real child environment without invoking an index or
            # installing packages into the test interpreter.
            (root / "pip.py").write_text(
                "import json, os, sys\n"
                "print(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n"
                "raise SystemExit(23)\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(wrapper), "install", "-e", ".[dev]"],
                cwd=root,
                env={
                    **os.environ,
                    "PYTHONPATH": str(root),
                    "MAKEFLAGS": " -- PYTHON_TEST_PATTERN=test_freebsd*.py",
                    "MFLAGS": "-j8",
                    "MAKEOVERRIDES": "PYTHON_TEST_PATTERN=test_freebsd*.py",
                    "GNUMAKEFLAGS": "-j8",
                    "CARGO_BUILD_JOBS": "2",
                    "PIP_NO_INDEX": "1",
                    "PIP_FIND_LINKS": str(root),
                },
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 23, completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertEqual(observed["argv"], ["install", "-e", ".[dev]"])
        environment = observed["env"]
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "GNUMAKEFLAGS"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["CARGO_BUILD_JOBS"], "2")
        self.assertEqual(environment["PIP_NO_INDEX"], "1")
        self.assertEqual(environment["PIP_FIND_LINKS"], str(root))


if __name__ == "__main__":
    unittest.main()
