"""Public authority, prompts, and portable source evidence agree on coverage."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.models import RecipeDocument
from literate_ai.adapters.source_evidence_validation import (
    SourceEvidenceValidationInputs,
)
from literate_ai.contracts import CYCLONEDX_SOURCE_SBOM_PATH
from literate_ai.generated_test_coverage import (
    COVERAGE_REQUEST_SCHEMA,
    PUBLIC_CALLABLE_CONTRACT_PATH,
    PUBLIC_CALLABLE_SIGNATURE_SCHEMA,
    ZERO_ARGUMENT_COVERAGE_MODE,
    require_generated_coverage_capacity,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_V2_SCHEMA,
    GeneratedTestSuiteError,
)
from tests.support.fixtures_test_coding_cli_generation import (
    flavor,
    recipe,
    write_generated_test_suite,
)


def public_document(value):
    return RecipeDocument.create(PUBLIC_CALLABLE_CONTRACT_PATH, json.dumps(value))


class ZeroArgumentCoverageEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "schema": COVERAGE_REQUEST_SCHEMA,
            "mode": ZERO_ARGUMENT_COVERAGE_MODE,
            "signature": {
                "schema": PUBLIC_CALLABLE_SIGNATURE_SCHEMA,
                "arity": 0,
                "optional": False,
                "variadic": False,
            },
        }
        self.base = replace(
            recipe(flavor("python")),
            documents=(
                RecipeDocument.create(
                    "openspec/spec.md",
                    "The public callable accepts no arguments and returns integer 7.\n",
                ),
            ),
        )
        self.document = public_document(self.request)
        self.acceptance = RecipeDocument.create(
            "acceptance/execution.json",
            json.dumps(
                {
                    "invocations": [{"arguments": [], "expected": "PRIVATE-ORACLE"}],
                    "result_shape": "integer",
                }
            ),
        )
        self.recipe = replace(
            self.base,
            documents=(*self.base.documents, self.document, self.acceptance),
        )
        self.authority = self.recipe.generated_test_coverage_authority
        self.suite = {
            "schema": GENERATED_TEST_SUITE_V2_SCHEMA,
            "recipe_identity": self.recipe.identity,
            "generation_mode": "major-rebuild",
            "coverage": self.authority.to_dict(),
            "cases": [
                {
                    "case_id": "generated-complete-domain",
                    "category": "example",
                    "specification_refs": ["openspec/spec.md"],
                    "arguments": [],
                    "expected_result": 7,
                }
            ],
        }

    def test_recipe_prompt_binds_public_domain_without_private_oracle(self):
        prompt = self.recipe.prompt()
        self.assertIn(self.document.identity, prompt)
        self.assertIn(GENERATED_TEST_SUITE_V2_SCHEMA, prompt)
        self.assertIn("Generate exactly one case", prompt)
        self.assertNotIn("Generate 3 through 256", prompt)
        self.assertNotIn("PRIVATE-ORACLE", prompt)
        self.assertNotEqual(self.recipe.identity, self.base.identity)
        self.assertIsNone(self.base.generated_test_coverage_authority)
        self.assertIn("Generate 3 through 256", self.base.prompt())
        changed = replace(
            self.document,
            content=self.document.content + "\n",
            identity=RecipeDocument.create(
                self.document.path, self.document.content + "\n"
            ).identity,
        )
        changed_recipe = replace(
            self.recipe,
            documents=(*self.base.documents, changed, self.acceptance),
        )
        self.assertNotEqual(changed_recipe.identity, self.recipe.identity)
        self.assertNotEqual(
            changed_recipe.generated_test_coverage_authority, self.authority
        )

    def test_acceptance_example_and_flavor_cannot_grant_exemption(self):
        ordinary = replace(self.base, documents=(*self.base.documents, self.acceptance))
        self.assertIsNone(ordinary.generated_test_coverage_authority)
        selected = replace(self.base.flavors[0], documents=(self.document,))
        self.assertIsNone(
            replace(self.base, flavors=(selected,)).generated_test_coverage_authority
        )

    def test_unknown_optional_variadic_and_nonzero_domains_fail_during_preparation(
        self,
    ):
        for change in ({"arity": 2}, {"optional": True}, {"variadic": True}):
            value = copy.deepcopy(self.request)
            value["signature"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(
                    self.base,
                    documents=(
                        *self.base.documents,
                        public_document(value),
                    ),
                )
        conflict = RecipeDocument.create(
            "acceptance/execution.json",
            json.dumps({"invocations": [{"arguments": ["PRIVATE-ARG"]}]}),
        )
        with self.assertRaisesRegex(
            ValueError, "coverage_interface_conflict"
        ) as caught:
            replace(
                self.recipe, documents=(*self.base.documents, self.document, conflict)
            )
        self.assertNotIn("PRIVATE-ARG", str(caught.exception))

    def test_portable_roundtrip_preserves_v1_and_revalidates_v2_against_current_inputs(
        self,
    ):
        default = SourceEvidenceValidationInputs.from_recipe(self.base)
        self.assertEqual(
            default.to_dict()["schema"], "literate-ai/source-evidence-validation@1"
        )
        self.assertNotIn("coverage", default.to_dict())
        self.assertEqual(
            SourceEvidenceValidationInputs.from_dict(default.to_dict()), default
        )
        current = SourceEvidenceValidationInputs.from_recipe(self.recipe)
        self.assertEqual(
            current.to_dict()["schema"], "literate-ai/source-evidence-validation@2"
        )
        wire = json.dumps(current.to_dict())
        self.assertNotIn("PRIVATE-ORACLE", wire)
        restored = SourceEvidenceValidationInputs.from_dict(json.loads(wire))
        self.assertEqual(restored.identity, current.identity)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_generated_test_suite(root, self.recipe)
            bom = (root / CYCLONEDX_SOURCE_SBOM_PATH).read_bytes()
            _, suite = restored.validate(bom, json.dumps(self.suite).encode())
            self.assertEqual(suite.coverage_authority, self.authority)
            corrupt = copy.deepcopy(self.suite)
            corrupt["coverage"]["public_interface_identity"] = "sha256:" + "f" * 64
            with self.assertRaisesRegex(
                GeneratedTestSuiteError, "current public interface"
            ):
                restored.validate(bom, json.dumps(corrupt).encode())
        stale = current.to_dict()
        stale["schema"] = "literate-ai/source-evidence-validation@1"
        with self.assertRaises(ValueError):
            SourceEvidenceValidationInputs.from_dict(stale)

    def test_project_minimum_is_checked_as_aggregate_distinct_input_capacity(self):
        require_generated_coverage_capacity((self.authority,), minimum_test_count=1)
        with self.assertRaisesRegex(ValueError, "project_minimum_incompatible"):
            require_generated_coverage_capacity((self.authority,), minimum_test_count=3)
        require_generated_coverage_capacity(
            (self.authority, self.authority), minimum_test_count=2
        )
        require_generated_coverage_capacity(
            (self.authority, None), minimum_test_count=257
        )
        with self.assertRaisesRegex(ValueError, "project_minimum_incompatible"):
            require_generated_coverage_capacity(
                (self.authority, None), minimum_test_count=258
            )

    def test_ambiguous_json_and_mixed_entrypoints_do_not_establish_authority(self):
        duplicate = self.document.content.replace(
            '"arity": 0', '"arity": 2, "arity": 0'
        )
        with self.assertRaisesRegex(ValueError, "invalid_public_signature"):
            replace(
                self.base,
                documents=(
                    *self.base.documents,
                    RecipeDocument.create(PUBLIC_CALLABLE_CONTRACT_PATH, duplicate),
                ),
            )
        with self.assertRaisesRegex(ValueError, "mixed_public_signatures_unsupported"):
            replace(
                self.recipe,
                required_entrypoint=None,
                required_entrypoints=("source/a.py", "source/b.py"),
            )

    def test_signature_is_a_real_loadable_literate_markdown_specification(self):
        from literate_ai.adapters.specifications.literate_markdown import (
            LiterateMarkdownProvider,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / self.document.path
            path.parent.mkdir(parents=True)
            path.write_text(self.document.content)
            (root / "spec.md").write_text(
                "---\nname: Singleton\nsummary: Return seven\nkind: component\n"
                "---\n# Behavior\n"
                "The public callable takes no arguments and returns 7.\n"
            )
            loaded = LiterateMarkdownProvider().load(
                root, ("spec.md", self.document.path), id_prefix="singleton"
            )
            self.assertEqual(
                loaded.specification_set.artifacts[1].identity.uri,
                self.document.identity,
            )
            loaded.require_unchanged(root)
