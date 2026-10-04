"""Retained inputs are bounded binary snapshots, not generated-source evidence."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_inputs as inputs
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
)


class RetainedProjectInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "empty").mkdir()
        (self.root / "src" / "main.py").write_bytes(b"print('retained')\n")
        (self.root / "assets").mkdir()
        (self.root / "assets" / "image.bin").write_bytes(bytes(range(256)) * 8192)
        self.limits = RetainedProjectLimits(100, 3_000_000, 4_000_000)
        self.roots = {"src": "source", "assets": "asset"}

    def discover(self):
        return inputs.discover_retained_project_inputs(
            self.root, self.roots, self.limits
        )

    def test_alias_and_unsafe_paths_rejected(self):
        for path in ("../secret", "C:secret", "src/CON", "src/a.", "src\\a"):
            with self.subTest(path=path), self.assertRaises((ValueError, TypeError)):
                RetainedProjectMember(path, 0, 0o644, "0" * 64, "source")
        # A case-insensitive filesystem cannot create this alias; wire validation
        # must reject it on every platform nevertheless.
        manifest = RetainedProjectMember("src/a", 0, 0o644, "0" * 64, "source")
        alias = RetainedProjectMember("SRC/A", 0, 0o644, "0" * 64, "source")
        with self.assertRaises(ValueError):
            RetainedProjectManifest(
                ("src",),
                tuple(sorted((manifest, alias), key=lambda m: m.path)),
                self.limits,
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "Host has no symlink support")
    def test_links_and_redirected_parents_rejected(self):
        (self.root / "src" / "link").symlink_to(self.root / "assets" / "image.bin")
        with self.assertRaises(ValueError):
            self.discover()
        (self.root / "src" / "link").unlink()
        redirected = self.base / "redirected"
        redirected.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            inputs.discover_retained_project_inputs(redirected, self.roots, self.limits)

    def test_mutation_during_capture_never_publishes_snapshot(self):
        manifest = self.discover()
        original = inputs._stream
        changed = False

        def mutate(root, name, limit, output=None):
            nonlocal changed
            result = original(root, name, limit, output)
            if output is not None and not changed:
                changed = True
                (self.root / "src" / "main.py").write_bytes(b"changed")
            return result

        with patch.object(inputs, "_stream", side_effect=mutate):
            with self.assertRaises(ValueError):
                inputs.capture_retained_project_inputs(
                    self.root, manifest, self.base / "snapshot"
                )
        self.assertFalse((self.base / "snapshot").exists())
        self.assertFalse(list(self.base.glob(".retained-*")))


if __name__ == "__main__":
    unittest.main()
