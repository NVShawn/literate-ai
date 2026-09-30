"""Regression coverage for portable Python generation authority."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = (
    ROOT
    / "skills"
    / "specification-to-source"
    / "python-portable-application"
    / "SKILL.md"
)
TEMPLATE_SKILL = (
    ROOT
    / "src"
    / "literate_ai"
    / "project_template"
    / "skills"
    / "specification-to-source"
    / "python-portable-application"
    / "SKILL.md"
)


class PythonGenerationSkillTests(unittest.TestCase):
    def test_python_discovery_is_directory_major(self) -> None:
        normalized = " ".join(SKILL.read_text(encoding="utf-8").split())

        self.assertIn("walk PATH directories in their declared order", normalized)
        self.assertIn("consider both `python` and `python3`", normalized)
        self.assertIn("never allow a preferred executable name", normalized)

    def test_zipapp_entrypoint_accepts_no_required_arguments(self) -> None:
        normalized = " ".join(SKILL.read_text(encoding="utf-8").split())

        self.assertIn("invokes `function()` with no positional arguments", normalized)
        self.assertIn("zero-argument archive wrapper", normalized)
        self.assertIn("Never point a zipapp directly at `_cli(argv)`", normalized)

    def test_declared_runner_must_discover_generated_native_tests(self) -> None:
        for skill in (SKILL, TEMPLATE_SKILL):
            with self.subTest(skill=skill):
                normalized = " ".join(skill.read_text(encoding="utf-8").split())
                self.assertIn("`unittest discover`", normalized)
                self.assertIn("`unittest.TestCase` method", normalized)
                self.assertIn("zero discovered tests is a failure", normalized.lower())
                self.assertIn("ordinary test target", normalized)
                self.assertIn("Source-only `tests` modules are unavailable", normalized)
                self.assertIn("against the exact exported path", normalized)

    def test_build_recipes_suppress_source_tree_bytecode(self) -> None:
        for skill in (SKILL, TEMPLATE_SKILL):
            with self.subTest(skill=skill):
                normalized = " ".join(skill.read_text(encoding="utf-8").split())
                self.assertIn("PYTHONDONTWRITEBYTECODE=1", normalized)
                self.assertIn("PYTHONPYCACHEPREFIX", normalized)
                self.assertIn("`__pycache__`", normalized)


if __name__ == "__main__":
    unittest.main()
