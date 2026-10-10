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
