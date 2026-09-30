"""Regression coverage for the LOW-layer Design-by-Contract skill paragraphs.

LOW-DBC-001 (docs/roadmap/0.5.0-low-design-by-contract-scoping.md) adds additive
Design-by-Contract guidance to the existing per-language implementation skills rather
than a new skill file. This asserts the paragraphs are present, name each language's
idiomatic mechanism, and that the `skills/` catalog copy stays byte-identical to the
`src/literate_ai/project_template/skills/` copy, matching the convention already
enforced for sibling skills (see test_make_generation_skill.py).
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _skill_pair(relative: str) -> tuple[Path, Path]:
    return (
        ROOT / "skills" / "specification-to-source" / relative / "SKILL.md",
        ROOT
        / "src"
        / "literate_ai"
        / "project_template"
        / "skills"
        / "specification-to-source"
        / relative
        / "SKILL.md",
    )


PYTHON_SKILLS = _skill_pair("python-portable-application")
RUST_SKILLS = _skill_pair("rust-portable-json-application")
GO_SKILLS = _skill_pair("go-portable-application")


class DesignByContractSkillParagraphTests(unittest.TestCase):
    def test_catalog_and_template_copies_are_byte_identical(self) -> None:
        for catalog, template in (PYTHON_SKILLS, RUST_SKILLS, GO_SKILLS):
            with self.subTest(catalog=catalog):
                self.assertEqual(catalog.read_bytes(), template.read_bytes())

    def test_python_skill_documents_assert_based_contracts(self) -> None:
        for skill in PYTHON_SKILLS:
            with self.subTest(skill=skill):
                normalized = " ".join(skill.read_text(encoding="utf-8").split())
                self.assertIn("internal/private helper functions", normalized)
                self.assertIn("add a plain `assert` statement", normalized)
                self.assertIn(
                    "Do not use `assert` to validate data that originates outside "
                    "the process",
                    normalized,
                )
                self.assertIn("stripped when run with `-O`", normalized)
                self.assertIn("`hypothesis`-based property test", normalized)
                self.assertIn(
                    "is an ordinary generated-test failure, not a new failure channel",
                    normalized,
                )

    def test_rust_skill_documents_assert_macro_contracts(self) -> None:
        for skill in RUST_SKILLS:
            with self.subTest(skill=skill):
                normalized = " ".join(skill.read_text(encoding="utf-8").split())
                self.assertIn("internal/private helper functions", normalized)
                self.assertIn("add an `assert!`/`assert_eq!`", normalized)
                self.assertIn(
                    "compiles unconditionally into both debug and release builds",
                    normalized,
                )
                self.assertIn("`debug_assert!`/`debug_assert_eq!`", normalized)
                self.assertIn(
                    "Do not use either macro to validate data that originates outside "
                    "the process",
                    normalized,
                )
                self.assertIn(
                    "is an ordinary generated-test failure, not a new failure channel",
                    normalized,
                )

    def test_go_skill_documents_panic_based_contracts(self) -> None:
        for skill in GO_SKILLS:
            with self.subTest(skill=skill):
                normalized = " ".join(skill.read_text(encoding="utf-8").split())
                self.assertIn("internal/private helper functions", normalized)
                self.assertIn("`panic()`", normalized)
                self.assertIn("reserved for programmer error", normalized)
                self.assertIn(
                    "Never use `panic` for data that originates outside the process",
                    normalized,
                )
                self.assertIn(
                    "is an ordinary generated-test failure, not a new failure channel",
                    normalized,
                )


if __name__ == "__main__":
    unittest.main()
