from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from packaging.markers import default_environment
from packaging.tags import sys_tags

from literate_ai.adapters.builders._process import (
    BoundedProcessResult,
    run_bounded_process,
)
from literate_ai.adapters.builders.python import BuildError, discover_python_toolchain
from literate_ai.adapters.dependencies.python_lock import (
    SCHEMA,
    parse_python_wheel_lock,
)
from literate_ai.adapters.dependencies.python_target import (
    _helper_sources,
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

    def test_helper_identity_is_deterministic_and_binds_exact_bytes(self):
        first = self.observe()
        self.assertEqual(first.identity, self.observe().identity)
        sources = _helper_sources()
        sources["__init__.py"] += b"\n# distinct probe helper bytes\n"
        with patch(
            "literate_ai.adapters.dependencies.python_target._helper_sources",
            return_value=sources,
        ):
            second = self.observe()
        self.assertNotEqual(first.probe_identity, second.probe_identity)
        self.assertNotEqual(first.identity, second.identity)
        self.assertEqual(first.environment, second.environment)

    def test_target_with_no_packaging_uses_owned_helper(self):
        environment = self.root / "empty-python"
        venv.EnvBuilder(with_pip=False).create(environment)
        executable = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        result = run_bounded_process(
            [
                str(executable),
                "-I",
                "-c",
                "import importlib.util; print(importlib.util.find_spec('packaging'))",
            ],
            cwd=self.root,
            environment=dict(os.environ),
            timeout_seconds=15,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            error_prefix="test.target",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"None")
        selected = discover_python_toolchain(pinned_command=str(executable))
        target = observe_python_wheel_target(selected, temporary_root=self.root)
        self.assertEqual(target.toolchain_identity, selected.identity)
        self.assertNotEqual(selected.identity, self.toolchain.identity)
        self.assertEqual(dict(target.environment), default_environment())

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

    def test_malformed_or_wrong_runtime_response_is_rejected(self):
        good = {
            "schema": "literate-ai/python-wheel-target-probe@1",
            "runtime": self.toolchain.runtime_executable,
            "implementation": self.toolchain.implementation,
            "version_info": list(self.toolchain.version_info),
            "environment": default_environment(),
            "tags": sorted(str(tag) for tag in sys_tags()),
        }
        documents = [
            b"not-json",
            json.dumps({**good, "runtime": "/wrong/runtime"}).encode(),
            json.dumps({**good, "extra": True}).encode(),
            json.dumps({**good, "tags": ["invalid"]}).encode(),
        ]
        for content in documents:
            with (
                self.subTest(content=content[:30]),
                patch(
                    "literate_ai.adapters.dependencies.python_target.run_bounded_process",
                    return_value=BoundedProcessResult(0, content, b""),
                ),
                self.assertRaises(DependencyObservationError),
            ):
                self.observe()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_or_stderr_probe_never_produces_target(self):
        for result in (
            BoundedProcessResult(1, b"", b""),
            BoundedProcessResult(0, b"{}", b"warning"),
        ):
            with (
                self.subTest(result=result),
                patch(
                    "literate_ai.adapters.dependencies.python_target.run_bounded_process",
                    return_value=result,
                ),
                self.assertRaises(DependencyObservationError),
            ):
                self.observe()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_timeout_and_toolchain_drift_fail_with_cleanup(self):
        with (
            patch(
                "literate_ai.adapters.dependencies.python_target.run_bounded_process",
                side_effect=BuildError("timeout", "probe timed out"),
            ),
            self.assertRaises(DependencyObservationError),
        ):
            self.observe()
        self.assertEqual(list(self.root.iterdir()), [])
        with (
            patch.object(
                type(self.toolchain),
                "require_unchanged",
                side_effect=BuildError("changed", "toolchain drift"),
            ),
            self.assertRaises(DependencyObservationError),
        ):
            self.observe()
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
