"""Published Draft 2020-12 wire schemas for reviewed semantic refinement."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from literate_ai.application.semantic_refinement import SemanticRefinementService
from tests.support import fixtures_test_semantic_refinement_service as refinement_fixtures
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = ROOT / "schemas" / "v2"
SCHEMA_FILE = V2_ROOT / "semantic-refinement.schema.json"
SCHEMA_ROOT = "urn:literate-ai:schema:v2:semantic-refinement-contracts"


class SemanticRefinementSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()
        self.request = refinement_fixtures._fixture()
        self.resolution = SemanticRefinementService().resolve(self.request)

    def test_every_public_contract_wire_validates_and_is_closed(self) -> None:
        contracts = (
            *self.request.conflicts,
            *self.request.proposals,
            *self.request.reviews,
            self.request,
            self.resolution,
        )
        observed: set[str] = set()
        for contract in contracts:
            with self.subTest(contract=type(contract).__name__):
                document = contract.to_dict()
                schema = document["schema"]
                observed.add(schema)
                self.schemas.validate(schema, document)
                with self.assertRaisesRegex(AssertionError, "unknown field"):
                    self.schemas.validate(schema, {**document, "implicit": True})

        catalog = json.loads((V2_ROOT / "index.json").read_text(encoding="utf-8"))
        entry = next(
            item for item in catalog["schemas"] if item["file"] == SCHEMA_FILE.name
        )
        self.assertEqual(entry["root_id"], SCHEMA_ROOT)
        self.assertEqual(observed, set(entry["public_ids"]))

    def test_nested_binding_and_acceptance_records_are_closed(self) -> None:
        review = self.request.reviews[0].to_dict()
        review["binding"]["unreviewed_input"] = True
        with self.assertRaisesRegex(AssertionError, "unknown field"):
            self.schemas.validate(review["schema"], review)

        resolution = self.resolution.to_dict()
        resolution["accepted"][0]["implicit_precedence"] = "nearest-wins"
        with self.assertRaisesRegex(AssertionError, "unknown field"):
            self.schemas.validate(resolution["schema"], resolution)

    def test_catalog_resource_ids_and_references_are_exact_and_resolvable(self) -> None:
        document = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(document)

        resources: set[str] = set()
        references: list[tuple[str, str | None]] = []

        def walk(value: object, base: str | None = None) -> None:
            if isinstance(value, dict):
                base = value.get("$id", base)
                if "$id" in value:
                    resources.add(value["$id"])
                if "$ref" in value:
                    references.append((value["$ref"], base))
                if value.get("type") == "object" and "properties" in value:
                    self.assertIs(value.get("additionalProperties"), False)
                for child in value.values():
                    walk(child, base)
            elif isinstance(value, list):
                for child in value:
                    walk(child, base)

        walk(document)
        for reference, base in references:
            self.schemas.resolve(reference, base)

        catalog = json.loads((V2_ROOT / "index.json").read_text(encoding="utf-8"))
        entry = next(
            item for item in catalog["schemas"] if item["file"] == SCHEMA_FILE.name
        )
        self.assertEqual(resources, {entry["root_id"], *entry["public_ids"]})


if __name__ == "__main__":
    unittest.main()
