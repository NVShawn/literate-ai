"""Built-in native package Flavor catalog policy."""

from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.adapters.flavor_markdown import parse_flavor_markdown

ROOT = Path(__file__).resolve().parents[2]


class PackageFlavorCatalogTests(unittest.TestCase):
    def test_catalog_text_files_have_clean_line_endings(self) -> None:
        """Inherited catalog bytes must pass downstream whitespace audits."""

        roots = (
            ROOT / "flavors",
            ROOT / "src/literate_ai/project_template/flavors",
        )
        for root in roots:
            for package_root in sorted(root.glob("package-*")):
                for path in sorted(package_root.rglob("*")):
                    if not path.is_file():
                        continue
                    with self.subTest(path=path.relative_to(ROOT).as_posix()):
                        payload = path.read_bytes()
                        self.assertFalse(payload.endswith(b"\n\n"))
                        for line_number, line in enumerate(payload.splitlines(), 1):
                            self.assertEqual(
                                line,
                                line.rstrip(b" \t"),
                                f"trailing whitespace on line {line_number}",
                            )

    def test_provider_names_targets_and_host_constraints_are_exact(self) -> None:
        expected = {
            "package-zip": ("package-zip", "zip", None),
            "package-apt": ("package-apt", "apt", "linux"),
            "package-brew": ("package-brew", "brew", "macos"),
            "package-cargo": ("package-cargo", "crates", "rust"),
            "package-chocolatey": ("package-chocolatey", "chocolatey", "windows"),
            "package-conan": ("package-conan", "conan", None),
            "package-npm": ("package-npm", "npm", "javascript"),
            "package-pip": ("package-pip", "pip", "python"),
            "package-winget": ("package-winget", "winget", "windows"),
        }

        for directory, (name, target, operating_system) in expected.items():
            with self.subTest(provider=directory):
                path = ROOT / "flavors" / directory / "flavor.md"
                flavor = parse_flavor_markdown(
                    path.read_bytes(), source=path.as_posix()
                )
                self.assertEqual(flavor.name, name)
                self.assertEqual(flavor.primary_axis, "packaging")
                self.assertEqual(flavor.target, target)
                constraints = tuple(flavor.secondary_constraints)
                if operating_system is None:
                    self.assertEqual(constraints, ())
                elif operating_system in {"javascript", "python", "rust"}:
                    self.assertEqual(
                        constraints,
                        (
                            {
                                "axis": "implementation.language-ecosystem",
                                "value": operating_system,
                                "optional": False,
                            },
                        ),
                    )
                else:
                    self.assertEqual(
                        constraints,
                        (
                            {
                                "axis": "platform.os",
                                "value": operating_system,
                                "optional": False,
                            },
                        ),
                    )
                self.assertEqual(
                    flavor.authoring_inputs[0].uri,
                    "../../skills/agent/package-artifacts/SKILL.md",
                )
                packaging_contributions = tuple(
                    contribution
                    for contribution in flavor.contributions
                    if contribution.kind == "packaging"
                )
                self.assertEqual(len(packaging_contributions), 1)
                self.assertEqual(
                    packaging_contributions[0].merge_operator, "keyed-union"
                )
                self.assertEqual(flavor.conflicts, ())

    def test_language_package_flavors_require_their_language(self) -> None:
        expected = {
            "package-cargo": ("rust", "flavor://literate-ai/lang-rust"),
            "package-npm": ("javascript", "flavor://literate-ai/lang-javascript"),
            "package-pip": ("python", "flavor://literate-ai/lang-python"),
        }
        for directory, (language, coordinate) in expected.items():
            with self.subTest(provider=directory):
                path = ROOT / "flavors" / directory / "flavor.md"
                flavor = parse_flavor_markdown(
                    path.read_bytes(), source=path.as_posix()
                )
                self.assertEqual(
                    flavor.secondary_constraints,
                    (
                        {
                            "axis": "implementation.language-ecosystem",
                            "value": language,
                            "optional": False,
                        },
                    ),
                )
                self.assertEqual(flavor.co_requisites, (coordinate,))

    def test_framework_and_initialized_template_catalogs_match(self) -> None:
        """The root catalog is the single source of truth for the shipped template.

        Every `literate-ai`-namespace root flavor directory must be byte-equal to its
        template counterpart; drift between the two catalogs is a defect, not a
        convention. Flavors from other namespaces (sample fixtures) stay root-only.
        """

        framework_root = ROOT / "flavors"
        template_root = ROOT / "src/literate_ai/project_template/flavors"
        expected_template_dirs = set()
        for item in sorted(framework_root.iterdir()):
            if not item.is_dir() or not (item / "flavor.md").is_file():
                continue
            flavor = parse_flavor_markdown(
                (item / "flavor.md").read_bytes(),
                source=(item / "flavor.md").as_posix(),
            )
            if flavor.namespace == "literate-ai":
                expected_template_dirs.add(item.name)
        template_dirs = {
            item.name
            for item in template_root.iterdir()
            if item.is_dir() and (item / "flavor.md").is_file()
        }
        self.assertEqual(template_dirs, expected_template_dirs)
        for directory in sorted(expected_template_dirs):
            for relative in sorted(
                path.relative_to(framework_root / directory).as_posix()
                for path in (framework_root / directory).rglob("*")
                if path.is_file()
            ):
                with self.subTest(flavor=directory, relative=relative):
                    framework = framework_root / directory / relative
                    template = template_root / directory / relative
                    self.assertEqual(framework.read_bytes(), template.read_bytes())


if __name__ == "__main__":
    unittest.main()
