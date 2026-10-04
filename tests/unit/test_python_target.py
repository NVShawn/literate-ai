from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from packaging.markers import default_environment
from packaging.tags import sys_tags

from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.dependencies.python_lock import (
    SCHEMA,
    parse_python_wheel_lock,
)
from literate_ai.adapters.dependencies.python_target import (
    observe_python_wheel_target,
)
from literate_ai.adapters.dependencies.types import DependencyObservationError


class PythonWheelTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.toolchain = discover_python_toolchain(pinned_command=sys.executable)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def observe(self, **kwargs):
        return observe_python_wheel_target(
            self.toolchain, temporary_root=self.root, **kwargs
        )

    def test_actual_target_and_exact_lock_matching(self):
        target = self.observe()
        self.assertEqual(dict(target.environment), default_environment())
        self.assertEqual(set(target.tags), {str(tag) for tag in sys_tags()})
        self.assertEqual(target.toolchain_identity, self.toolchain.identity)
        document = {
            "schema": SCHEMA,
            "environment": dict(target.environment),
            "tags": list(target.tags),
            "requirements": [],
            "packages": [],
        }
        target.require_lock(parse_python_wheel_lock(json.dumps(document)))
        document["environment"]["platform_machine"] = "different-target"
        with self.assertRaises(DependencyObservationError):
            target.require_lock(parse_python_wheel_lock(json.dumps(document)))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_python_startup_hooks_are_not_loaded(self):
        injection = self.root / "injection"
        injection.mkdir()
        marker = self.root / "executed"
        (injection / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        self.observe(
            environment={
                **os.environ,
                "PYTHONPATH": str(injection),
                "PYTHONSTARTUP": str(injection / "sitecustomize.py"),
            }
        )
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
