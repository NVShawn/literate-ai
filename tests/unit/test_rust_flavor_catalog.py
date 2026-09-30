"""The repository's Rust Flavor remains a pinned, composable generation input."""

from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.cli.generation import load_flavor_catalog

REPOSITORY = Path(__file__).resolve().parents[2]


class RustFlavorCatalogTests(unittest.TestCase):
    def test_exact_skill_does_not_block_javascript(self) -> None:
        (rust,) = load_flavor_catalog((REPOSITORY / "flavors" / "lang-rust",))

        self.assertEqual(rust.coordinate_uri, "flavor://literate-ai/lang-rust")
        self.assertEqual(rust.axis, "implementation.language-ecosystem")
        self.assertEqual(rust.value, "rust")
        self.assertEqual(
            tuple(skill.skill_id for skill in rust.skills),
            ("rust-portable-json-application",),
        )
        self.assertNotIn("flavor://literate-ai/lang-javascript", rust.conflicts)


if __name__ == "__main__":
    unittest.main()
