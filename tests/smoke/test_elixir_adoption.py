"""Public adoption plans retain Elixir evidence without executing source."""

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.project_initialization import plan_convert


class ElixirAdoptionTests(unittest.TestCase):
    def test_portable_make_project_is_ready_and_plan_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.exs").write_text(
                'raise "must not execute"\n', encoding="utf-8"
            )
            (root / "Makefile").write_text(
                "all:\n\telixir main.exs\n", encoding="utf-8"
            )
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            report = plan_convert(root, default_branch="main")
            self.assertEqual(report["readiness"], "ready")
            self.assertEqual(report["languages"], ["elixir"])
            self.assertIn(
                "flavor://literate-ai/lang-elixir", report["proposed_flavors"]
            )
            self.assertIn("flavor://literate-ai/build-make", report["proposed_flavors"])
            self.assertEqual(report["catalog_gaps"], [])
            self.assertFalse(report["writes"])
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_mix_marker_does_not_invent_a_supported_build_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mix.exs").write_text(
                'raise "must not execute"\n', encoding="utf-8"
            )
            report = plan_convert(root, default_branch="main")
            self.assertEqual(report["readiness"], "blocked-no-driver")
            self.assertEqual(report["languages"], ["elixir"])
            self.assertEqual(report["proposed_wrapper_stages"], [])
            self.assertIn(
                "flavor://literate-ai/lang-elixir", report["proposed_flavors"]
            )
            candidate = report["build_root_candidates"][0]
            self.assertEqual(candidate["markers"], ["mix.exs"])
            self.assertEqual(candidate["authority"], "candidate-only")

    def test_detect_repo_flavors_identifies_elixir_without_evaluating_sources(self):
        from literate_ai.adapters.project_initialization import detect_repo_flavors

        for marker in ("app.ex", "main.exs", "mix.exs"):
            with (
                self.subTest(marker=marker),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / marker).write_text(
                    'raise "must not execute"\n', encoding="utf-8"
                )
                selectors = detect_repo_flavors(root)
                self.assertIn("flavor://literate-ai/lang-elixir", selectors)
                self.assertNotIn("flavor://literate-ai/lang-python", selectors)
                self.assertNotIn("flavor://literate-ai/build-cargo", selectors)

    def test_elixir_detection_preserves_mixed_language_evidence(self):
        from literate_ai.adapters.project_initialization import (
            detected_language_flavors,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for marker in ("main.cpp", "main.py"):
                (root / marker).write_text("", encoding="utf-8")
            (root / "lib").mkdir()
            (root / "lib" / "app.ex").write_text("", encoding="utf-8")
            self.assertEqual(
                detected_language_flavors(root),
                [
                    "flavor://literate-ai/lang-cpp",
                    "flavor://literate-ai/lang-python",
                    "flavor://literate-ai/lang-elixir",
                ],
            )

    def test_elixir_detection_ignores_generated_and_hidden_sources(self):
        from literate_ai.adapters.project_initialization import (
            detected_language_flavors,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("_build", ".cache"):
                (root / name).mkdir()
                (root / name / "main.exs").write_text("", encoding="utf-8")
            self.assertEqual(detected_language_flavors(root), [])
