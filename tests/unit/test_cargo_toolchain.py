"""Cargo host-toolchain discovery and drift tests."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.builders import BuildError, discover_cargo_toolchain


@unittest.skipIf(os.name == "nt", "fixture uses a POSIX executable script")
class CargoToolchainTests(unittest.TestCase):
    def _cargo(self, root: Path, version: str, *, name: str = "cargo") -> Path:
        path = root / name
        path.write_text(
            f"#!/bin/sh\nprintf '%s\\n' 'cargo {version} (fixture 2026-01-01)'\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_path_selects_and_binds_compatible_cargo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self._cargo(root, "1.82.0")
            environment = {"PATH": str(root)}

            toolchain = discover_cargo_toolchain(environment)

            self.assertEqual(toolchain.command, (str(selected),))
            self.assertEqual(toolchain.version_info, (1, 82, 0))
            toolchain.require_unchanged(environment)

    def test_explicit_non_cargo_fails_without_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pinned = self._cargo(root, "1.82.0", name="pinned")
            pinned.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'not cargo'\n", encoding="utf-8"
            )
            pinned.chmod(pinned.stat().st_mode | stat.S_IXUSR)
            self._cargo(root, "1.82.0")

            with self.assertRaisesRegex(BuildError, "not Cargo"):
                discover_cargo_toolchain(
                    {"PATH": str(root)}, pinned_command=(str(pinned),)
                )

    def test_launcher_byte_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self._cargo(root, "1.82.0")
            environment = {"PATH": str(root)}
            toolchain = discover_cargo_toolchain(environment)

            selected.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'cargo 1.82.0 (changed)'\n",
                encoding="utf-8",
            )
            selected.chmod(selected.stat().st_mode | stat.S_IXUSR)

            with self.assertRaisesRegex(BuildError, "bytes changed"):
                toolchain.require_unchanged(environment)

    def test_minimum_version_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._cargo(root, "1.60.0")

            with self.assertRaisesRegex(BuildError, "older than"):
                discover_cargo_toolchain({"PATH": str(root)})


if __name__ == "__main__":
    unittest.main()
