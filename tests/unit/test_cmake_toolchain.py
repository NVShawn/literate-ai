"""CMake host-toolchain discovery and drift tests."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.builders import BuildError, discover_cmake_toolchain


@unittest.skipIf(os.name == "nt", "fixture uses a POSIX executable script")
class CMakeToolchainTests(unittest.TestCase):
    def _cmake(self, root: Path, version: str, *, name: str = "cmake") -> Path:
        path = root / name
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' 'cmake version {version}'\n"
            "printf '%s\\n' 'fixture'\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_path_order_selects_the_first_compatible_cmake(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            selected = self._cmake(first, "3.27.4")
            self._cmake(second, "3.28.0")

            toolchain = discover_cmake_toolchain(
                {"PATH": os.pathsep.join((str(first), str(second)))},
            )

            self.assertEqual(toolchain.command, (str(selected),))
            self.assertEqual(toolchain.version_info, (3, 27, 4))
            toolchain.require_unchanged(
                {"PATH": os.pathsep.join((str(first), str(second)))}
            )

    def test_explicit_non_cmake_fails_without_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pinned = root / "pinned"
            pinned.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'not cmake 2026'\n",
                encoding="utf-8",
            )
            pinned.chmod(pinned.stat().st_mode | stat.S_IXUSR)
            fallback = root / "fallback"
            fallback.mkdir()
            self._cmake(fallback, "3.28.0")

            with self.assertRaisesRegex(BuildError, "not CMake"):
                discover_cmake_toolchain(
                    {"PATH": str(fallback)}, pinned_command=(str(pinned),)
                )

    def test_launcher_byte_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self._cmake(root, "3.28.0")
            environment = {"PATH": str(root)}
            toolchain = discover_cmake_toolchain(environment)

            selected.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'cmake version 3.28.0'\n# changed\n",
                encoding="utf-8",
            )
            selected.chmod(selected.stat().st_mode | stat.S_IXUSR)

            with self.assertRaisesRegex(BuildError, "bytes changed"):
                toolchain.require_unchanged(environment)

    def test_minimum_version_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._cmake(root, "3.10.0")

            with self.assertRaisesRegex(BuildError, "older than"):
                discover_cmake_toolchain({"PATH": str(root)})


if __name__ == "__main__":
    unittest.main()
