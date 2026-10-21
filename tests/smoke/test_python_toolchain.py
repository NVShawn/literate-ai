"""Python toolchain discovery follows exact pins and ordered host preferences."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import BuildError, discover_python_toolchain
from literate_ai.adapters.builders import python as python_builder_module
from tests.support.symlinks import symlinks_followable


def _install_python_alias(directory: Path, name: str) -> Path:
    if os.name == "nt":
        # A relocated base python.exe loses its DLL/stdlib home. Real venv
        # redirectors retain that home even with this deliberately isolated PATH.
        scripts = directory / "Scripts"
        if not (directory / "pyvenv.cfg").exists():
            venv.EnvBuilder(with_pip=False, symlinks=False).create(directory)
            launcher = scripts / "python.exe"
            alias = scripts / f"{name}.exe"
            launcher.rename(alias)
        else:
            launcher = next(
                p for p in scripts.glob("python*.exe") if p.name != "pythonw.exe"
            )
            alias = scripts / f"{name}.exe"
            shutil.copy2(launcher, alias)
        return alias
    alias = directory / name
    runtime = Path(sys.executable)
    if symlinks_followable(directory):
        alias.symlink_to(runtime)
    else:
        # Copy where links cannot be created or followed (WinError 1463 over
        # Windows OpenSSH), so discovery still sees a real interpreter.
        shutil.copy2(runtime, alias)
        alias.chmod(alias.stat().st_mode | 0o111)
    return alias


class PythonToolchainDiscoveryTests(unittest.TestCase):
    def test_versioned_only_runtime_is_probed_and_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = _install_python_alias(root, "python3.12")
            environment = {"PATH": str(command.parent)}
            selected = discover_python_toolchain(environment, minimum_version=(3, 0))

            self.assertEqual(selected.command, (str(command),))
            selected.require_unchanged(environment)

    def test_path_directory_precedes_unversioned_name_in_later_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            command = _install_python_alias(first, "python3.12")
            later = _install_python_alias(second, "python3")

            selected = discover_python_toolchain(
                {"PATH": os.pathsep.join((str(command.parent), str(later.parent)))},
                minimum_version=(3, 0),
            )

            self.assertEqual(selected.command, (str(command),))

    def test_aliases_precede_numerically_ordered_versioned_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _install_python_alias(root, "python3.9")
            newer = _install_python_alias(root, "python3.12")
            self.assertEqual(
                discover_python_toolchain(
                    {"PATH": str(newer.parent)}, minimum_version=(3, 0)
                ).command,
                (str(newer),),
            )
            alias = _install_python_alias(root, "python")
            # Windows PATH lookup may return the PATHEXT spelling (.EXE).
            self.assertEqual(
                tuple(
                    map(
                        Path,
                        discover_python_toolchain(
                            {"PATH": str(alias.parent)}, minimum_version=(3, 0)
                        ).command,
                    )
                ),
                (alias,),
            )

    def test_versioned_discovery_does_not_admit_config_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _install_python_alias(root, "python3.12-config")
            with patch.object(python_builder_module, "_probe_python_command") as probe:
                with self.assertRaises(BuildError) as error:
                    discover_python_toolchain({"PATH": str(config.parent)})

            self.assertEqual(
                error.exception.code, "builder.python_toolchain_unavailable"
            )
            probe.assert_not_called()

    def test_versioned_name_cannot_substitute_for_actual_runtime_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = _install_python_alias(root, "python3.999")
            with self.assertRaises(BuildError) as error:
                discover_python_toolchain(
                    {"PATH": str(command.parent)}, required_version=(3, 999)
                )

            self.assertEqual(
                error.exception.__cause__.code, "builder.python_version_unsupported"
            )

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
            _install_python_alias(root, "python3.12")
            environment = {"PATH": str(pinned.parent), "PYTHON": str(pinned)}
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
