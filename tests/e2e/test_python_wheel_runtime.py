from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_WHEEL_RUNTIME_DRIVER,
)
from literate_ai.contracts import LITAI_SMOKE_MODE_FLAG
from tests.support import fixtures_test_standard_python_evidence as fixtures


class PythonWheelRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.StandardPythonEvidenceTests.setUpClass()

    def setUp(self):
        self.fixture = fixtures.StandardPythonEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.export = self.fixture.root / "application"
        self.export.mkdir()
        self.entrypoint = self.export / "main.py"
        self.entrypoint.write_text(
            "import example,helper,json,sys\n"
            "print(json.dumps([example.__file__,helper.__file__,sys.argv,"
            "sys.flags.isolated,sys.flags.no_site,sys.dont_write_bytecode]))\n",
            encoding="utf-8",
        )
        self.argv = (
            *self.fixture.fixture.toolchain.command,
            "-I",
            "-S",
            "-B",
            "-c",
            STANDARD_PYTHON_WHEEL_RUNTIME_DRIVER,
            str(self.fixture.root),
            str(self.export),
            "tree",
            "main.py",
            LITAI_SMOKE_MODE_FLAG,
        )

    def run_driver(self, argv=None, environment=None):
        return subprocess.run(
            argv or self.argv,
            cwd=self.fixture.root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def test_launch_imports_retained_packages_and_ignores_ambient_startup(self):
        evidence = self.fixture.retain()
        self.fixture.verify(evidence)
        ambient = self.fixture.root / "ambient"
        ambient.mkdir()
        for filename in ("example.py", "helper.py", "sitecustomize.py"):
            (ambient / filename).write_text(
                "raise RuntimeError('ambient startup')\n", encoding="utf-8"
            )
        result = self.run_driver(environment={**os.environ, "PYTHONPATH": str(ambient)})
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(
            all(
                Path(path).is_relative_to((evidence.payload_root / "site").resolve())
                for path in value[:2]
            )
        )
        self.assertEqual(value[2], [str(self.entrypoint), LITAI_SMOKE_MODE_FLAG])
        self.assertEqual(value[3:], [1, 1, True])
        self.assertEqual(list(self.fixture.root.rglob("__pycache__")), [])

    def test_missing_payload_does_not_fall_back_to_ambient_packages(self):
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verified Python payload is absent", result.stderr)
