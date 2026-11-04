"""Compiler overrides must preserve the native fixture's process environment."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.builders import cpp
from tests.support import fixtures_worker_alignment as fixture


class CodingCliFixtureEnvironmentTests(unittest.TestCase):
    def compile_environment(self, family, overrides, configured, platform):
        tool = SimpleNamespace(
            command=("fixture-compiler",), family=family, environment=overrides
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.dict(os.environ, configured, clear=True),
                patch.object(fixture, "discover_cpp_toolchain", return_value=tool),
                patch.object(cpp, "os", SimpleNamespace(name=platform)),
                patch.object(
                    fixture,
                    "run_bounded_process",
                    return_value=SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
                ) as run,
            ):
                fixture.build_coding_cli(root)
            self.assertEqual(run.call_args.kwargs["timeout_seconds"], 300)
            self.assertEqual(run.call_args.kwargs["stdout_limit_bytes"], 1024 * 1024)
            self.assertEqual(run.call_args.kwargs["stderr_limit_bytes"], 1024 * 1024)
            return run.call_args.kwargs["environment"]

    def test_msvc_overrides_retain_temp_and_system_with_casefold_precedence(self):
        configured = {
            "TEMP": "owned-temp",
            "TMP": "owned-tmp",
            "SystemRoot": "owned-system",
            "Path": "old-path",
            "PATH": "duplicate-old-path",
            "Include": "old-include",
        }
        vendor = (("PATH", "vendor-path"), ("INCLUDE", "vendor-include"))
        environment = self.compile_environment("msvc", vendor, configured, "nt")
        self.assertEqual(
            {name.casefold(): value for name, value in environment.items()},
            {
                "temp": "owned-temp",
                "tmp": "owned-tmp",
                "systemroot": "owned-system",
                "path": "vendor-path",
                "include": "vendor-include",
            },
        )
        self.assertEqual(sum(name.casefold() == "path" for name in environment), 1)

    def test_nonempty_posix_overrides_preserve_configured_environment(self):
        configured = {"TMPDIR": "owned-temp", "SOURCE_SENTINEL": "retained"}
        vendor = (("PATH", "vendor-path"),)
        environment = self.compile_environment("gcc", vendor, configured, "posix")
        self.assertEqual(environment, dict(configured, PATH="vendor-path"))


if __name__ == "__main__":
    unittest.main()
