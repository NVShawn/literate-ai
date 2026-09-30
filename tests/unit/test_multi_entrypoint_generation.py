from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.models import (
    CodingCliError,
    CodingCliSourceGenerator,
    GenerationRecipe,
    RecipeDocument,
    RecipeFlavor,
    RecipeSkill,
)
from literate_ai.contracts import ContentIdentity, ContentReference

TEST_COMPONENT_LOCK_IDENTITY = ContentIdentity.parse_uri("sha256:" + "b" * 64)


def _planning_skill() -> RecipeSkill:
    content = json.dumps(
        {
            "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
            "skill_id": "full-stack-planning",
            "version": "1.0.0",
            "title": "Full-Stack Planning",
            "stages": ["plan", "generate"],
            "dependencies": [],
            "instructions": "Generate every declared application role.",
            "limitations": ["Do not invent undeclared roles."],
            "trust": "fixture-reviewed",
        },
        sort_keys=True,
    ).encode()
    reference = ContentReference(
        "specification-to-source-skill",
        "skills/full-stack-planning.json",
        ContentIdentity.parse_uri("sha256:" + hashlib.sha256(content).hexdigest()),
    )
    return RecipeSkill.from_reference(reference, content, source="test fixture")


def _full_stack_recipe(
    *, documents: tuple[RecipeDocument, ...] | None = None
) -> GenerationRecipe:
    return GenerationRecipe(
        "full-stack-recipe",
        "release-dashboard",
        documents
        or (RecipeDocument.create("openspec/spec.md", "Build both roles.\n"),),
        TEST_COMPONENT_LOCK_IDENTITY,
        (
            RecipeFlavor(
                "implementation-javascript",
                "implementation.language-ecosystem",
                "javascript",
                (RecipeDocument.create("javascript/spec.md", "Use Node.js.\n"),),
                slot_ids=("frontend-language",),
            ),
            RecipeFlavor(
                "implementation-rust",
                "implementation.language-ecosystem",
                "rust",
                (RecipeDocument.create("rust/spec.md", "Use Rust.\n"),),
                slot_ids=("backend-language",),
            ),
        ),
        skills=(_planning_skill(),),
        required_entrypoints=(
            "source/backend/main.rs",
            "source/frontend/main.js",
        ),
    )


class MultiEntrypointGenerationTests(unittest.TestCase):
    def test_recipe_binds_both_roles_and_documents_host_observed_stages(self) -> None:
        # The two-stage host-observed protocol is Component specification authority:
        # it must reach the prompt through the locked specification documents, while
        # the framework envelope itself stays domain-neutral.
        protocol = (
            "### Requirement: Use a host-observed two-stage JSON protocol\n\n"
            "The trusted authorized host runner invokes the compiled Rust backend "
            "first with argv[1], then the Node frontend with process.argv[2].\n"
            "Fields: `release`, `release_status`, `services`, `passed_checks`, "
            "`failed_checks`, `total_checks`, `blocked_services`, "
            "`review_services`, `total_risk_points`, `pass_rate_basis_points`, "
            "`top_risk_service`.\n"
            "Frontend tests must exercise this exact object handoff and never a "
            "wrapper-array API. Generate no Makefile or Cargo manifest. Backend "
            "tests embed in main.rs; frontend tests live in "
            "source/frontend/main.test.js.\n"
        )
        recipe = _full_stack_recipe(
            documents=(
                RecipeDocument.create("openspec/spec.md", "Build both roles.\n"),
                RecipeDocument.create("protocol/spec.md", protocol),
            ),
        )

        self.assertEqual(
            recipe.all_required_entrypoints,
            ("source/backend/main.rs", "source/frontend/main.js"),
        )
        prompt = recipe.prompt()
        self.assertIn("`source/backend/main.rs`", prompt)
        self.assertIn("`source/frontend/main.js`", prompt)
        # Protocol authority arrives through the specification document section.
        self.assertIn("trusted authorized host runner", prompt)
        self.assertIn("process.argv[2]", prompt)
        self.assertIn("argv[1]", prompt)
        for field in (
            "release",
            "release_status",
            "services",
            "passed_checks",
            "failed_checks",
            "total_checks",
            "blocked_services",
            "review_services",
            "total_risk_points",
            "pass_rate_basis_points",
            "top_risk_service",
        ):
            self.assertIn(f"`{field}`", prompt)
        self.assertIn("exact object handoff", prompt)
        self.assertIn("source/frontend/main.test.js", prompt)
        # The envelope adds no domain topology of its own.
        envelope_end = prompt.index("## Base specification")
        envelope = prompt[:envelope_end]
        for domain_vocabulary in (
            "top_risk_service",
            "pass_rate_basis_points",
            "process.argv[2]",
            "trusted composite host builder",
            "main.test.js",
        ):
            self.assertNotIn(domain_vocabulary, envelope)

    def test_recipe_rejects_mixed_singular_and_plural_entrypoint_contracts(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "cannot mix singular and plural"):
            GenerationRecipe(
                "ambiguous-entrypoint-recipe",
                "release-dashboard",
                (RecipeDocument.create("openspec/spec.md", "Build both roles.\n"),),
                TEST_COMPONENT_LOCK_IDENTITY,
                required_entrypoint="source/main.rs",
                required_entrypoints=("source/frontend/main.js",),
                skills=(_planning_skill(),),
            )

    def test_collection_fails_closed_until_every_entrypoint_exists(self) -> None:
        recipe = _full_stack_recipe()
        collector = object.__new__(CodingCliSourceGenerator)
        collector.maximum_generated_files = 16
        collector.maximum_generated_entries = 32
        collector.maximum_generated_bytes = 1024 * 1024
        collector.maximum_generated_path_length = 256
        collector.maximum_generated_depth = 8

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "source" / "frontend" / "main.js"
            frontend.parent.mkdir(parents=True)
            frontend.write_text("console.log('{}');\n", encoding="utf-8")

            with self.assertRaises(CodingCliError) as raised:
                collector._collect(root, recipe.all_required_entrypoints)
            self.assertEqual(raised.exception.code, "coding_cli.entrypoint_missing")
            self.assertIn("source/backend/main.rs", str(raised.exception))

            backend = root / "source" / "backend" / "main.rs"
            backend.parent.mkdir(parents=True)
            backend.write_text("fn main() {}\n", encoding="utf-8")
            files = collector._collect(root, recipe.all_required_entrypoints)

        self.assertEqual(
            set(files),
            {"source/backend/main.rs", "source/frontend/main.js"},
        )


if __name__ == "__main__":
    unittest.main()
