"""Python toolchain discovery follows exact pins and ordered host preferences."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import BuildError, discover_python_toolchain
from literate_ai.adapters.builders import python as python_builder_module
from literate_ai.adapters.builders._process import BoundedProcessResult


def _install_python_alias(directory: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    alias = directory / f"{name}{suffix}"
    runtime = Path(
        getattr(sys, "_base_executable", sys.executable)
        if os.name == "nt"
        else sys.executable
    )
    try:
        alias.symlink_to(runtime)
    except OSError:
        shutil.copy2(runtime, alias)
        alias.chmod(alias.stat().st_mode | 0o111)
    return alias


def _path_with_runtime(*directories: Path) -> str:
    paths = [str(directory) for directory in directories]
    if os.name == "nt":
        # A copied Windows python.exe still loads python3.dll from its installation
        # directory. Keep that directory last so the fixture aliases retain PATH
        # precedence while remaining genuinely executable.
        runtime = Path(getattr(sys, "_base_executable", sys.executable))
        paths.append(str(runtime.parent))
    return os.pathsep.join(paths)


def _current_probe():
    environment = dict(os.environ)
    selected = discover_python_toolchain(
        environment,
        pinned_command=(sys.executable,),
        minimum_version=(3, 0),
    )
    return python_builder_module._PythonProbe(
        runtime_executable=selected.runtime_executable,
        runtime_digest=selected.runtime_digest,
        implementation=selected.implementation,
        version=selected.version,
        version_info=selected.version_info,
        cache_tag=selected.cache_tag,
    )


class PythonToolchainDiscoveryTests(unittest.TestCase):
    def test_selected_runtime_revalidates_without_drift(self) -> None:
        environment = dict(os.environ)
        selected = discover_python_toolchain(
            environment,
            pinned_command=(sys.executable,),
            minimum_version=(3, 0),
        )

        selected.require_unchanged(environment)

    def test_path_directory_order_wins_across_python_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            early = root / "early"
            late = root / "late"
            early.mkdir()
            late.mkdir()
            preferred = _install_python_alias(early, "python")
            _install_python_alias(late, "python3")

            selected = discover_python_toolchain(
                {"PATH": _path_with_runtime(early, late)},
                minimum_version=(3, 0),
            )

            self.assertEqual(Path(selected.command[0]), preferred)

    def test_bad_python3_falls_back_to_python_in_the_same_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = _install_python_alias(root, "python3")
            fallback = _install_python_alias(root, "python")
            real_process = python_builder_module._run_bounded_process

            def reject_first(command, **kwargs):
                if Path(command[0]) == bad:
                    return BoundedProcessResult(0, b"not-python\n", b"")
                return real_process(command, **kwargs)

            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                side_effect=reject_first,
            ):
                selected = discover_python_toolchain(
                    {"PATH": _path_with_runtime(root)}, minimum_version=(3, 0)
                )

            self.assertEqual(Path(selected.command[0]), fallback)

    def test_python2_probe_record_is_rejected(self) -> None:
        runtime = str(Path(sys.executable).resolve(strict=True))
        payload = {
            "cache_tag": "cpython-27",
            "executable": runtime,
            "implementation": "cpython",
            "version": "2.7.18",
            "version_info": [2, 7, 18, "final", 0],
        }
        result = BoundedProcessResult(
            0,
            (
                python_builder_module._PYTHON_PROBE_PREFIX
                + json.dumps(payload, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode(),
            b"",
        )
        with patch.object(
            python_builder_module,
            "_run_bounded_process",
            return_value=result,
        ):
            with self.assertRaises(BuildError) as error:
                python_builder_module._probe_python_command(
                    (runtime,),
                    dict(os.environ),
                    timeout_seconds=1,
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                )

        self.assertEqual(error.exception.code, "builder.python_version_unsupported")

    def test_unpinned_python2_candidate_falls_back_to_python3(self) -> None:
        baseline = _current_probe()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            early = root / "early"
            late = root / "late"
            early.mkdir()
            late.mkdir()
            _install_python_alias(early, "python3")
            fallback = _install_python_alias(late, "python")
            with patch.object(
                python_builder_module,
                "_probe_python_command",
                side_effect=(
                    BuildError(
                        "builder.python_version_unsupported",
                        "candidate is Python 2",
                    ),
                    baseline,
                ),
            ):
                selected = discover_python_toolchain(
                    {"PATH": os.pathsep.join((str(early), str(late)))},
                    minimum_version=(3, 0),
                )

            self.assertEqual(Path(selected.command[0]), fallback)

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

    def test_default_minimum_skips_python_older_than_framework_flavor(self) -> None:
        baseline = _current_probe()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            early = root / "early"
            late = root / "late"
            early.mkdir()
            late.mkdir()
            _install_python_alias(early, "python3")
            selected_path = _install_python_alias(late, "python")
            old = replace(
                baseline,
                version="3.9.99",
                version_info=(3, 9, 99, "final", 0),
            )
            with patch.object(
                python_builder_module,
                "_probe_python_command",
                side_effect=(old, baseline),
            ):
                selected = discover_python_toolchain(
                    {"PATH": os.pathsep.join((str(early), str(late)))}
                )

            self.assertEqual(Path(selected.command[0]), selected_path)

    def test_required_version_prefix_skips_other_python3_versions(self) -> None:
        baseline = _current_probe()
        required = baseline.version_info[:2]
        other_minor = 0 if required[1] != 0 else 1
        other = replace(
            baseline,
            version=f"3.{other_minor}.0",
            version_info=(3, other_minor, 0, "final", 0),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            early = root / "early"
            late = root / "late"
            early.mkdir()
            late.mkdir()
            _install_python_alias(early, "python3")
            selected_path = _install_python_alias(late, "python")
            with patch.object(
                python_builder_module,
                "_probe_python_command",
                side_effect=(other, baseline),
            ):
                selected = discover_python_toolchain(
                    {"PATH": os.pathsep.join((str(early), str(late)))},
                    minimum_version=(3, 0),
                    required_version=required,
                )

            self.assertEqual(Path(selected.command[0]), selected_path)

    def test_duplicate_path_entries_do_not_reprobe_one_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _install_python_alias(root, "python3")
            with patch.object(
                python_builder_module,
                "_probe_python_command",
                side_effect=BuildError(
                    "builder.python_version_failed", "invalid candidate"
                ),
            ) as probe:
                with self.assertRaises(BuildError) as error:
                    discover_python_toolchain(
                        {"PATH": os.pathsep.join((str(root), str(root)))},
                        minimum_version=(3, 0),
                    )

            self.assertEqual(
                error.exception.code, "builder.python_toolchain_unavailable"
            )
            self.assertEqual(probe.call_count, 1)

    @unittest.skipUnless(os.name == "posix", "symlink retarget test is POSIX-specific")
    def test_symlink_invocation_is_retained_and_retargeting_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alias = root / "python3"
            alias.symlink_to(Path(sys.executable))
            selected = discover_python_toolchain(
                {"PATH": str(root)}, minimum_version=(3, 0)
            )
            self.assertEqual(Path(selected.command[0]), alias)
            self.assertEqual(
                Path(selected.launcher_executable),
                Path(sys.executable).resolve(strict=True),
            )

            alias.unlink()
            alias.symlink_to(Path("/bin/false"))
            with self.assertRaises(BuildError) as error:
                selected.require_unchanged({"PATH": str(root)})

            self.assertEqual(error.exception.code, "builder.python_toolchain_changed")

    def test_pinned_arguments_and_ambient_python_hooks_are_exact(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = "/untrusted/imports"
        environment["PYTHONSTARTUP"] = "/untrusted/startup.py"
        selected = discover_python_toolchain(
            environment,
            pinned_command=(sys.executable, "-X", "utf8"),
            minimum_version=(3, 0),
        )
        plain = discover_python_toolchain(
            environment,
            pinned_command=(sys.executable,),
            minimum_version=(3, 0),
        )

        self.assertEqual(selected.command[1:], ("-X", "utf8"))
        self.assertNotEqual(selected.identity, plain.identity)
        controlled = python_builder_module.controlled_python_environment(environment)
        self.assertNotIn("PYTHONPATH", controlled)
        self.assertNotIn("PYTHONSTARTUP", controlled)


if __name__ == "__main__":
    unittest.main()
