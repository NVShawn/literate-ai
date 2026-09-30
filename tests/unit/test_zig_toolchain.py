"""Zig and zig-cc Standard host-toolchain discovery."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.builders import BuildError, discover_zig_toolchain
from literate_ai.adapters.standard_project import (
    StandardCommandProjectionError,
    _discover_toolchain,
)
from literate_ai.contracts import ToolchainConstraint


def _write_python_zig(path: Path, version: str) -> Path:
    path.write_text(
        "import sys\n"
        f"version = {version!r}\n"
        "if sys.argv[1:] == ['version']:\n"
        "    print(version)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    return path


class ZigToolchainDiscoveryTests(unittest.TestCase):
    def test_missing_path_fails_closed_without_compiling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(BuildError) as raised:
                discover_zig_toolchain({"PATH": temporary})
        self.assertEqual(raised.exception.code, "builder.zig_toolchain_unavailable")
        self.assertIn("not found", str(raised.exception))

    def test_pinned_python_fake_binds_version_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = _write_python_zig(Path(temporary) / "zig.py", "0.13.0")
            environment = {"PATH": ""}
            toolchain = discover_zig_toolchain(
                environment,
                pinned_command=(sys.executable, str(script)),
            )
            self.assertEqual(
                Path(toolchain.command[0]).resolve(),
                Path(sys.executable).resolve(),
            )
            self.assertEqual(toolchain.command[1], str(script))
            self.assertEqual(toolchain.version, "0.13.0")
            self.assertEqual(toolchain.version_info, (0, 13, 0))
            self.assertTrue(toolchain.identity.startswith("sha256:"))
            toolchain.require_unchanged(environment)

    def test_pinned_zig_cc_keeps_driver_and_probes_zig_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = _write_python_zig(Path(temporary) / "zig.py", "0.14.0-dev.1+abc")
            toolchain = discover_zig_toolchain(
                {"PATH": ""},
                pinned_command=(sys.executable, str(script), "cc"),
                default_command=("zig", "cc"),
            )
            self.assertEqual(toolchain.command[-1], "cc")
            self.assertEqual(toolchain.version_info, (0, 14, 0))
            language = discover_zig_toolchain(
                {"PATH": ""},
                pinned_command=(sys.executable, str(script)),
            )
            self.assertNotEqual(toolchain.identity, language.identity)

    def test_non_zig_banner_fails_without_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pinned = _write_python_zig(root / "pinned.py", "not a compiler")
            fallback = _write_python_zig(root / "zig.py", "0.13.0")
            with self.assertRaises(BuildError) as raised:
                discover_zig_toolchain(
                    {"PATH": str(root)},
                    pinned_command=(sys.executable, str(pinned)),
                )
            self.assertEqual(raised.exception.code, "builder.zig_version_unsupported")
            self.assertIn("not Zig", str(raised.exception))
            recovered = discover_zig_toolchain(
                {"PATH": ""},
                pinned_command=(sys.executable, str(fallback)),
            )
            self.assertEqual(recovered.version_info, (0, 13, 0))

    def test_minimum_version_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = _write_python_zig(Path(temporary) / "zig.py", "0.11.0")
            with self.assertRaises(BuildError) as raised:
                discover_zig_toolchain(
                    {"PATH": ""},
                    pinned_command=(sys.executable, str(script)),
                )
            self.assertEqual(raised.exception.code, "builder.zig_version_unsupported")
            self.assertIn("older than", str(raised.exception))

    def test_required_version_prefix_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = _write_python_zig(Path(temporary) / "zig.py", "0.13.0")
            with self.assertRaises(BuildError) as raised:
                discover_zig_toolchain(
                    {"PATH": ""},
                    pinned_command=(sys.executable, str(script)),
                    required_version=(0, 14),
                )
            self.assertEqual(raised.exception.code, "builder.zig_version_unsupported")
            self.assertIn("does not match", str(raised.exception))


@unittest.skipIf(os.name == "nt", "fixture uses a POSIX executable script")
class ZigPathToolchainTests(unittest.TestCase):
    def _zig(self, root: Path, version: str, *, name: str = "zig") -> Path:
        path = root / name
        path.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "version" ]; then\n'
            f"  printf '%s\\n' '{version}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_path_selects_and_binds_compatible_zig(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self._zig(root, "0.13.0")
            environment = {"PATH": str(root)}
            toolchain = discover_zig_toolchain(environment)
            self.assertEqual(toolchain.command, (str(selected),))
            self.assertEqual(toolchain.version_info, (0, 13, 0))
            toolchain.require_unchanged(environment)

    def test_default_zig_cc_appends_the_cc_driver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self._zig(root, "0.13.0")
            toolchain = discover_zig_toolchain(
                {"PATH": str(root)},
                default_command=("zig", "cc"),
            )
            self.assertEqual(toolchain.command, (str(selected), "cc"))

    def test_launcher_byte_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self._zig(root, "0.13.0")
            environment = {"PATH": str(root)}
            toolchain = discover_zig_toolchain(environment)
            selected.write_text(
                "#!/bin/sh\nprintf '%s\\n' '0.13.0'\n# changed\n",
                encoding="utf-8",
            )
            selected.chmod(selected.stat().st_mode | stat.S_IXUSR)
            with self.assertRaisesRegex(BuildError, "bytes changed"):
                toolchain.require_unchanged(environment)


class ZigStandardHostAdapterTests(unittest.TestCase):
    def test_unknown_toolchain_still_fails_closed(self) -> None:
        with self.assertRaises(StandardCommandProjectionError) as raised:
            _discover_toolchain("cobol", None, {"PATH": ""})
        self.assertEqual(
            raised.exception.code, "standard_command.toolchain_unsupported"
        )

    def test_missing_zig_reports_install_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(StandardCommandProjectionError) as raised:
                _discover_toolchain(
                    "zig",
                    ToolchainConstraint("zig", minimum_version=(0, 13)),
                    {"PATH": temporary},
                )
        self.assertEqual(
            raised.exception.code, "standard_command.zig_toolchain_unavailable"
        )
        self.assertIn("https://ziglang.org/download/", str(raised.exception))

    def test_missing_zig_cc_reports_install_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(StandardCommandProjectionError) as raised:
                _discover_toolchain(
                    "zig-cc",
                    ToolchainConstraint("zig-cc", ("zig", "cc"), (0, 13)),
                    {"PATH": temporary},
                )
        self.assertEqual(
            raised.exception.code,
            "standard_command.zig_cc_toolchain_unavailable",
        )
        self.assertIn("https://ziglang.org/download/", str(raised.exception))

    def test_constraint_remediation_uri_is_appended(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(StandardCommandProjectionError) as raised:
                _discover_toolchain(
                    "zig",
                    ToolchainConstraint(
                        "zig",
                        remediation="install Zig from the project pin",
                        remediation_uri="https://ziglang.org/download/",
                    ),
                    {"PATH": temporary},
                )
        self.assertIn("install Zig from the project pin", str(raised.exception))
        self.assertIn("https://ziglang.org/download/", str(raised.exception))

    def test_admitted_zig_and_zig_cc_use_the_same_fake(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = _write_python_zig(Path(temporary) / "zig.py", "0.13.0")
            environment = {"PATH": ""}
            zig = _discover_toolchain(
                "zig",
                ToolchainConstraint(
                    "zig",
                    (sys.executable, str(script)),
                    (0, 13),
                ),
                environment,
            )
            zig_cc = _discover_toolchain(
                "zig-cc",
                ToolchainConstraint(
                    "zig-cc",
                    (sys.executable, str(script), "cc"),
                    (0, 13),
                ),
                environment,
            )
            self.assertEqual(zig.version_info, (0, 13, 0))
            self.assertEqual(zig_cc.command[-1], "cc")
            self.assertNotEqual(zig.identity, zig_cc.identity)

    def test_invalid_fake_is_not_a_successful_compile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = _write_python_zig(Path(temporary) / "zig.py", "not zig")
            with self.assertRaises(StandardCommandProjectionError) as raised:
                _discover_toolchain(
                    "zig",
                    ToolchainConstraint(
                        "zig",
                        (sys.executable, str(script)),
                    ),
                    {"PATH": ""},
                )
            self.assertEqual(
                raised.exception.code, "standard_command.zig_toolchain_invalid"
            )
            self.assertNotIn("compiled", str(raised.exception).casefold())


if __name__ == "__main__":
    unittest.main()
