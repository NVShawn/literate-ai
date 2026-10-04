"""Exact-byte retention after installed-wheel smoke qualification."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.release_files import file_identity
from scripts.wheel_smoke import _retain_qualified_wheel


class WheelSmokeCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.wheel = self.root / "literate_ai-1.1.0-py3-none-any.whl"
        # Custody-only fixture; these bytes are not an installable distribution.
        self.wheel.write_bytes(b"exact qualification fixture\n")
        self.identity = file_identity(self.wheel)

    def retain(self) -> Path:
        return _retain_qualified_wheel(
            self.root,
            self.wheel,
            revision="a" * 40,
            version="1.1.0",
            expected_identity=self.identity,
        )

    def test_changed_tested_wheel_is_not_marked_qualified(self) -> None:
        self.wheel.write_bytes(b"changed after qualification\n")
        with self.assertRaisesRegex(RuntimeError, "changed during qualification"):
            self.retain()
        self.assertEqual(list(self.root.rglob("manifest.json")), [])

    def test_changed_copied_bytes_are_not_marked_qualified(self) -> None:
        def corrupt_copy(source: Path, destination: Path) -> None:
            destination.write_bytes(b"changed during retention\n")

        with (
            mock.patch("scripts.wheel_smoke.shutil.copy2", side_effect=corrupt_copy),
            self.assertRaisesRegex(ValueError, "missing or changed"),
        ):
            self.retain()
        self.assertEqual(list(self.root.rglob("manifest.json")), [])


if __name__ == "__main__":
    unittest.main()
