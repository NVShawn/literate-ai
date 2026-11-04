"""Controlled vendor-boundary checks; native CMD evidence is separate."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import cpp
from literate_ai.adapters.builders.python import BuildError


class MsvcEnvironmentArgumentTests(unittest.TestCase):
    def fixture(self, root: Path, host: str, target: str):
        root = root.resolve()
        installation = root / "vendor with spaces"
        vc = installation / "VC"
        compiler = vc / "Tools/MSVC/14.29.30133/bin" / f"Host{host}" / target / "cl.exe"
        setup = vc / "Auxiliary/Build/vcvarsall.bat"
        developer = installation / "Common7/Tools/VsDevCmd.bat"
        interpreter = root / "windows/System32/cmd.exe"
        for path in (compiler, setup, developer, interpreter):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"controlled OS boundary")
        return compiler, {"SystemRoot": str(root / "windows")}, setup, developer

    def test_selected_targets_preserve_complete_toolset_option_and_limits(self):
        for host, target, expected_arguments, expected_call in (
            ("x64", "x64", ["x64", "14.29"], "-vcvars_ver=%~3"),
            (
                "arm64",
                "arm64",
                ["arm64", "arm64", "14.29"],
                "-arch=%~2 -host_arch=%~3 -vcvars_ver=%~4",
            ),
            (
                "x64",
                "arm64",
                ["arm64", "amd64", "14.29"],
                "-arch=%~2 -host_arch=%~3 -vcvars_ver=%~4",
            ),
        ):
            with (
                self.subTest(host=host, target=target),
                tempfile.TemporaryDirectory() as directory,
            ):
                compiler, configured, setup, developer = self.fixture(
                    Path(directory), host, target
                )
                captured = []

                def run(arguments, *, captured=captured, **kwargs):
                    captured.append((arguments, Path(arguments[4]).read_text(), kwargs))
                    return subprocess.CompletedProcess(
                        arguments,
                        0,
                        b"INCLUDE=fixture\nLIB=fixture\nLIBPATH=fixture\nPATH=fixture\n",
                        b"",
                    )

                with (
                    patch.object(
                        cpp,
                        "observe_pe_architecture",
                        return_value="x86_64" if host == "x64" else "arm64",
                    ),
                    patch.object(cpp, "_run_bounded_process", side_effect=run),
                ):
                    observed = cpp._windows_msvc_environment(
                        compiler,
                        configured,
                        requested_target="x86_64" if target == "x64" else "arm64",
                        timeout_seconds=0.25,
                        stdout_limit_bytes=4096,
                        stderr_limit_bytes=4096,
                    )
                arguments, wrapper, limits = captured[0]
                self.assertEqual(arguments[6:], expected_arguments)
                self.assertEqual(
                    Path(arguments[5]), setup if target == host == "x64" else developer
                )
                self.assertIn(expected_call, wrapper)
                self.assertEqual(limits["timeout_seconds"], 0.25)
                self.assertEqual(limits["stdout_limit_bytes"], 4096)
                self.assertEqual(limits["stderr_limit_bytes"], 4096)
                self.assertEqual(
                    dict(observed),
                    {name: "fixture" for name in ("INCLUDE", "LIB", "LIBPATH", "PATH")},
                )

    def test_preconfigured_values_do_not_replace_missing_vendor_variables(self):
        with tempfile.TemporaryDirectory() as directory:
            compiler, configured, _, _ = self.fixture(Path(directory), "x64", "x64")
            configured.update(
                {name: "inherited" for name in ("INCLUDE", "LIB", "LIBPATH", "PATH")}
            )
            original = compiler.read_bytes()
            with (
                patch.object(cpp, "observe_pe_architecture", return_value="x86_64"),
                patch.object(
                    cpp,
                    "_run_bounded_process",
                    return_value=subprocess.CompletedProcess(
                        [], 0, b"INCLUDE=fixture\nLIBPATH=fixture\nPATH=fixture\n", b""
                    ),
                ),
                self.assertRaises(BuildError) as refused,
            ):
                cpp._windows_msvc_environment(
                    compiler, configured, requested_target="x86_64"
                )
            self.assertEqual(
                refused.exception.code, "builder.cpp_msvc_environment_unavailable"
            )
            self.assertEqual(compiler.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
