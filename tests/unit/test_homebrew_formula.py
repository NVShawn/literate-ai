"""Homebrew formula for installing literate-ai itself."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from literate_ai.version import DISTRIBUTION_VERSION
from scripts.render_homebrew_formula import render_homebrew_formula

REPO = Path(__file__).resolve().parents[2]


class HomebrewFormulaTests(unittest.TestCase):
    def test_formula_uses_python_virtualenv_and_avoids_codegraph(self) -> None:
        text = render_homebrew_formula()
        self.assertIn("include Language::Python::Virtualenv", text)
        self.assertIn("virtualenv_install_with_resources", text)
        self.assertIn(DISTRIBUTION_VERSION, text)
        self.assertNotIn("codegraph", text.lower())
        self.assertNotIn('depends_on "node"', text)
        self.assertIn(f'LITERATE_AI_VERSION = "{DISTRIBUTION_VERSION}"', text)
        self.assertIn("version LITERATE_AI_VERSION", text)
        self.assertIn("v#{LITERATE_AI_VERSION}.tar.gz", text)
        policy = json.loads(
            (REPO / "literate.release.json").read_text(encoding="utf-8")
        )
        self.assertIn(policy["provider"]["repository"], text)
        shipped = (REPO / "packaging" / "homebrew" / "literate-ai.rb").read_text(
            encoding="utf-8"
        )
        self.assertEqual(shipped, text)
        formula_binding = next(
            item
            for item in policy["version_mirrors"]
            if item["path"] == "packaging/homebrew/literate-ai.rb"
        )
        self.assertEqual(formula_binding["format"], "quoted-constant")
        self.assertEqual(formula_binding["selectors"], ["LITERATE_AI_VERSION"])
