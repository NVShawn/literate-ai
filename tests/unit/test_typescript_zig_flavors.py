"""TypeScript, Zig, and Zig-cc Flavors resolve independently."""

from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.project_initialization import (
    FLAVOR_SELECTOR_CANONICAL_NAMES,
    KNOWN_FLAVOR_SELECTORS,
    flavor_selector_directory,
)
from literate_ai.cli.generation import load_flavor_catalog

REPO = Path(__file__).resolve().parents[2]


class TypeScriptZigFlavorTests(unittest.TestCase):
    def test_selectors_and_directories_are_axis_qualified(self) -> None:
        self.assertEqual(
            FLAVOR_SELECTOR_CANONICAL_NAMES.get("lang-typescript"),
            "flavor://literate-ai/lang-typescript",
        )
        self.assertEqual(
            FLAVOR_SELECTOR_CANONICAL_NAMES.get("typescript"),
            "flavor://literate-ai/lang-typescript",
        )
        self.assertEqual(
            KNOWN_FLAVOR_SELECTORS.get("lang-typescript"),
            "implementation.language-ecosystem",
        )
        self.assertEqual(flavor_selector_directory("+typescript"), "lang-typescript")
        self.assertEqual(flavor_selector_directory("+zig"), "lang-zig")
        self.assertEqual(
            flavor_selector_directory("+toolchain-zig-cc"), "toolchain-zig-cc"
        )

    def test_zig_language_and_zig_cc_are_independent(self) -> None:
        zig = parse_flavor_markdown(
            (REPO / "flavors" / "lang-zig" / "flavor.md").read_bytes(),
            source="flavors/lang-zig/flavor.md",
        )
        zig_cc = parse_flavor_markdown(
            (REPO / "flavors" / "toolchain-zig-cc" / "flavor.md").read_bytes(),
            source="flavors/toolchain-zig-cc/flavor.md",
        )
        self.assertEqual(zig.primary_axis, "implementation.language-ecosystem")
        self.assertEqual(zig_cc.primary_axis, "toolchain")
        self.assertNotIn("flavor://literate-ai/lang-zig", zig_cc.conflicts)
        self.assertNotIn("flavor://literate-ai/toolchain-zig-cc", zig.conflicts)
        catalog = load_flavor_catalog(
            (
                REPO / "flavors" / "lang-zig",
                REPO / "flavors" / "toolchain-zig-cc",
            )
        )
        self.assertEqual(
            {item.coordinate_uri for item in catalog},
            {
                "flavor://literate-ai/lang-zig",
                "flavor://literate-ai/toolchain-zig-cc",
            },
        )

    def test_catalog_and_template_trees_are_byte_identical(self) -> None:
        names = ("lang-typescript", "lang-zig", "toolchain-zig-cc")
        skills = (
            "typescript-portable-application",
            "zig-portable-application",
        )
        template_root = REPO / "src" / "literate_ai" / "project_template"
        for name in names:
            source = REPO / "flavors" / name
            template = template_root / "flavors" / name
            relatives = sorted(
                path.relative_to(source) for path in source.rglob("*") if path.is_file()
            )
            self.assertEqual(
                relatives,
                sorted(
                    path.relative_to(template)
                    for path in template.rglob("*")
                    if path.is_file()
                ),
            )
            for relative in relatives:
                self.assertEqual(
                    (source / relative).read_bytes(),
                    (template / relative).read_bytes(),
                )
        for skill_id in skills:
            catalog = (
                REPO / "skills" / "specification-to-source" / skill_id / "SKILL.md"
            )
            template = (
                template_root
                / "skills"
                / "specification-to-source"
                / skill_id
                / "SKILL.md"
            )
            self.assertEqual(catalog.read_bytes(), template.read_bytes())
