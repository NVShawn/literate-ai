"""Python toolchain discovery follows exact pins and ordered host preferences."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import BuildError, discover_python_toolchain
from literate_ai.adapters.builders import python as python_builder_module
from tests.support.symlinks import symlinks_followable


def _install_python_alias(directory: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    alias = directory / f"{name}{suffix}"
    runtime = Path(
        getattr(sys, "_base_executable", sys.executable)
        if os.name == "nt"
        else sys.executable
    )
    if symlinks_followable(directory):
        alias.symlink_to(runtime)
    else:
        # Copy where links cannot be created or followed (WinError 1463 over
        # Windows OpenSSH), so discovery still sees a real interpreter.
        shutil.copy2(runtime, alias)
        alias.chmod(alias.stat().st_mode | 0o111)
    return alias


class PythonToolchainDiscoveryTests(unittest.TestCase):
    def test_selected_runtime_revalidates_without_drift(self) -> None:
        environment = dict(os.environ)
        selected = discover_python_toolchain(
            environment,
            pinned_command=(sys.executable,),
            minimum_version=(3, 0),
        )

        selected.require_unchanged(environment)

    def test_explicit_python_pin_fails_without_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pinned = _install_python_alias(root, "python3")
            _install_python_alias(root, "python")
            environment = {"PATH": str(root), "PYTHON": str(pinned)}
            with patch.object(
                python_builder_module,
                "_probe_python_command",
                side_effect=BuildError(
                    "builder.python_version_failed", "configured command is invalid"
                ),
            ) as probe:
                with self.assertRaises(BuildError) as error:
                    discover_python_toolchain(environment, minimum_version=(3, 0))

            self.assertEqual(error.exception.code, "builder.python_version_failed")
            self.assertEqual(probe.call_count, 1)


if __name__ == "__main__":
    unittest.main()
