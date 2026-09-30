"""Exact-byte retention after installed-wheel smoke qualification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.release_files import file_identity, validate_release_files
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

    def test_retains_the_exact_bytes_and_validates_manifest(self) -> None:
        manifest = self.retain()
        value = json.loads(manifest.read_bytes())
        validate_release_files(
            manifest.parent,
            value,
            revision="a" * 40,
            version="1.1.0",
            required_roles=("wheel",),
        )
        wheel = manifest.parent / value["files"][0]["path"]
        self.assertEqual(wheel.read_bytes(), self.wheel.read_bytes())
        self.assertEqual(value["files"][0]["identity"], self.identity)
        self.assertNotIn(str(self.root), manifest.read_text(encoding="utf-8"))
        self.wheel.unlink()
        self.assertTrue(wheel.is_file())

    def test_repeated_qualification_preserves_previous_custody(self) -> None:
        first = self.retain()
        first_bytes = first.read_bytes()
        second = self.retain()
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(second.read_bytes(), first_bytes)

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

    def test_linked_output_custody_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.root / "_build").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.retain()
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
