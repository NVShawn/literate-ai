"""Regression coverage for JavaScript and C++ ecosystem layout skills."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from literate_ai.adapters.models.coding_cli import (
    GenerationRecipe,
    RecipeDocument,
    RecipeFlavor,
    RecipeSkill,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    FlavorAxis,
    HashAlgorithm,
)

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "skills/specification-to-source"


def _skill(name: str) -> RecipeSkill:
    path = CATALOG / name / "SKILL.md"
    content = path.read_bytes()
    reference = ContentReference(
        "specification-to-source-skill",
        path.relative_to(ROOT).as_posix(),
        ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()),
    )
    return RecipeSkill.from_reference(reference, content, source=path.as_posix())


def _javascript_recipe(*, package_npm: bool) -> GenerationRecipe:
    catalog = tuple(
        _skill(name)
        for name in (
            "portable-specification-planning",
            "portable-application-implementation",
            "javascript-portable-json-application",
            "repository-layout",
            "javascript-ecosystem",
        )
    )
    by_id = {skill.skill_id: skill for skill in catalog}
    flavors = [
        RecipeFlavor(
            "lang-javascript",
            FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
            "javascript",
            (
                RecipeDocument.create(
                    "flavors/lang-javascript/openspec/spec.md",
                    (ROOT / "flavors/lang-javascript/openspec/spec.md").read_text(
                        encoding="utf-8"
                    ),
                ),
            ),
            coordinate_uri="flavor://literate-ai/lang-javascript",
            skills=(by_id["javascript-portable-json-application"],),
        )
    ]
    if package_npm:
        flavors.append(
            RecipeFlavor(
                "package-npm",
                FlavorAxis.PACKAGING,
                "npm",
                (
                    RecipeDocument.create(
                        "flavors/package-npm/openspec/spec.md",
                        (ROOT / "flavors/package-npm/openspec/spec.md").read_text(
                            encoding="utf-8"
                        ),
                    ),
                ),
                coordinate_uri="flavor://literate-ai/package-npm",
                skills=(by_id["javascript-ecosystem"],),
            )
        )
    return GenerationRecipe(
        "javascript-package-authority",
        "javascript-package-authority",
        (RecipeDocument.create("component.md", "Implement the application.\n"),),
        ContentIdentity.parse_uri("sha256:" + "c" * 64),
        tuple(flavors),
        required_entrypoint="source/main.js",
        skill_catalog=catalog,
    )


class EcosystemLayoutSkillTests(unittest.TestCase):
    def test_catalog_and_template_skills_are_byte_identical(self) -> None:
        for name in (
            "javascript-portable-json-application",
            "javascript-ecosystem",
            "cpp-ecosystem",
        ):
            with self.subTest(skill=name):
                catalog = ROOT / "skills/specification-to-source" / name / "SKILL.md"
                template = (
                    ROOT
                    / "src/literate_ai/project_template/skills/specification-to-source"
                    / name
                    / "SKILL.md"
                )
                self.assertEqual(catalog.read_bytes(), template.read_bytes())

    def test_javascript_and_cpp_keep_derived_state_out_of_source(self) -> None:
        javascript = (
            ROOT / "skills/specification-to-source/javascript-ecosystem/SKILL.md"
        ).read_text(encoding="utf-8")
        cpp = (
            ROOT / "skills/specification-to-source/cpp-ecosystem/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("node_modules", javascript)
        self.assertIn("OBJ_DIR", javascript)
        self.assertIn("package.json", javascript)
        self.assertIn("package-npm", javascript)
        self.assertIn("complete matching lockfile", javascript)
        self.assertIn("CycloneDX source SBOM", javascript)
        self.assertIn("authorized lifecycle alone detects", javascript)
        self.assertNotIn("Detect `npm`", javascript)
        self.assertIn("include/<project>/", cpp)
        self.assertIn("OBJ_DIR", cpp)
        self.assertIn("build-system-neutral", cpp)

    def test_dependency_free_javascript_prompt_keeps_direct_self_check(self) -> None:
        prompt = _javascript_recipe(package_npm=False).prompt()

        self.assertIn("Skill: `javascript-portable-json-application@1.4.10`", prompt)
        self.assertNotIn("Skill: `javascript-ecosystem@", prompt)
        self.assertIn(
            "Do not declare or depend on an npm package unless the `package-npm` "
            "Flavor is selected.",
            prompt,
        )
        self.assertIn("Without `package-npm`, use built-in modules only.", prompt)
        self.assertIn("run `node source/main.js --litai-test` directly", prompt)
        self.assertIn("bundled `--litai-test` and `--litai-smoke`", prompt)
        self.assertIn("require.main === module", prompt)
        self.assertNotIn("Do not use npm packages", prompt)
        self.assertNotIn("locate `node` on `PATH`", prompt)
        self.assertNotIn("Detect `npm`", prompt)

    def test_package_npm_prompt_is_exact_and_has_no_no_npm_conflict(self) -> None:
        prompt = _javascript_recipe(package_npm=True).prompt()

        self.assertIn("Skill: `javascript-portable-json-application@1.4.10`", prompt)
        self.assertIn("Skill: `javascript-ecosystem@1.0.1`", prompt)
        self.assertIn("exact package.json manifest", prompt)
        self.assertIn("complete matching package-lock.json", prompt)
        self.assertIn("CycloneDX source SBOM", prompt)
        self.assertIn("authorized lifecycle admits it", prompt)
        self.assertIn("authorized lifecycle alone detects", prompt)
        self.assertNotIn("Do not use npm packages", prompt)
        self.assertNotIn("using built-in modules only", prompt)
        self.assertNotIn("locate `node` on `PATH`", prompt)
        self.assertNotIn("Detect `npm`", prompt)


if __name__ == "__main__":
    unittest.main()
