"""Explicit compiler-member budgets retain exact byte custody and hard ceilings."""

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.retained_cargo_runtime_files import (
    capture_retained_cargo_runtime_files,
)
from literate_ai.projects import PinnedInputClosureError


class RetainedCargoRuntimeBudgetTests(unittest.TestCase):
    def test_exact_member_boundary_and_later_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            member = root / "compiler-runtime"
            member.write_bytes(b"x" * 17)
            with self.assertRaises(PinnedInputClosureError):
                capture_retained_cargo_runtime_files((root,), maximum_file_bytes=16)
            captured = capture_retained_cargo_runtime_files(
                (root,), maximum_file_bytes=17
            )
            self.assertEqual(captured.closure.maximum_file_bytes, 17)
            self.assertEqual(captured.closure.maximum_total_bytes, 512 * 1024**2)
            captured.require_unchanged()
            member.write_bytes(b"y" * 17)
            with self.assertRaises(PinnedInputClosureError):
                captured.require_unchanged()

    def test_default_and_explicit_policy_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "compiler-runtime").write_bytes(b"small")
            default = capture_retained_cargo_runtime_files((root,))
            self.assertEqual(default.closure.maximum_file_bytes, 128 * 1024**2)
            compiler = capture_retained_cargo_runtime_files(
                (root,), maximum_file_bytes=256 * 1024**2
            )
            self.assertEqual(compiler.closure.maximum_total_bytes, 512 * 1024**2)
            for invalid in (0, -1, True, 1.5, 256 * 1024**2 + 1):
                with self.subTest(limit=invalid), self.assertRaises(ValueError):
                    capture_retained_cargo_runtime_files(
                        (root,), maximum_file_bytes=invalid
                    )
