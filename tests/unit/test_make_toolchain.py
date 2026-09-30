"""GNU Make host-toolchain discovery and drift tests."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.builders import BuildError, discover_make_toolchain


@unittest.skipIf(os.name == "nt", "fixture uses a POSIX executable script")
class MakeToolchainTests(unittest.TestCase):
    def _make(self, root: Path, version: str, *, name: str = "make") -> Path:
        path = root / name
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' 'GNU Make {version}'\n"
            "printf '%s\\n' 'fixture'\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_path_order_selects_the_first_compatible_make(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            selected = self._make(first, "4.3")
            self._make(second, "4.4")

            toolchain = discover_make_toolchain(
                {"PATH": os.pathsep.join((str(first), str(second)))},
            )

            self.assertEqual(toolchain.command, (str(selected),))
            self.assertEqual(toolchain.version_info, (4, 3, 0))
            toolchain.require_unchanged(
                {"PATH": os.pathsep.join((str(first), str(second)))}
            )

    def test_explicit_non_gnu_make_fails_without_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pinned = root / "pinned"
            pinned.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'BSD make 2026'\n",
                encoding="utf-8",
            )
            pinned.chmod(pinned.stat().st_mode | stat.S_IXUSR)
            fallback = root / "fallback"
            fallback.mkdir()
            self._make(fallback, "4.4")

            with self.assertRaisesRegex(BuildError, "not GNU Make"):
                discover_make_toolchain(
                    {"PATH": str(fallback)}, pinned_command=(str(pinned),)
                )

    def test_launcher_byte_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self._make(root, "4.4")
            environment = {"PATH": str(root)}
            toolchain = discover_make_toolchain(environment)

            selected.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'GNU Make 4.4'\n# changed\n",
                encoding="utf-8",
            )
            selected.chmod(selected.stat().st_mode | stat.S_IXUSR)

            with self.assertRaisesRegex(BuildError, "bytes changed"):
                toolchain.require_unchanged(environment)


if __name__ == "__main__":
    unittest.main()
