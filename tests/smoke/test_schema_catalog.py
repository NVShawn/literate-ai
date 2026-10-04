"""Machine-readable schema-catalog and current public wire conformance tests."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2] / "schemas" / "v1"
V2_ROOT = Path(__file__).parents[2] / "schemas" / "v2"


class SchemaCatalog:
    def __init__(self, root: Path = ROOT) -> None:
        self.catalog = json.loads((root / "index.json").read_text())
        self.documents = {
            path.name: json.loads(path.read_text())
            for path in sorted(root.glob("*.schema.json"))
        }
        self.resources: dict[str, dict] = {}
        for document in self.documents.values():
            self._collect(document)
        self.catalog_resources = frozenset(self.resources)
        if root == ROOT:
            for path in sorted(V2_ROOT.glob("*.schema.json")):
                self._collect(json.loads(path.read_text()))

    def _collect(self, value) -> None:
        if isinstance(value, dict):
            if "$id" in value:
                if value["$id"] in self.resources:
                    raise AssertionError(f"duplicate schema ID {value['$id']}")
                self.resources[value["$id"]] = value
            for child in value.values():
                self._collect(child)
        elif isinstance(value, list):
            for child in value:
                self._collect(child)

    def resolve(self, reference: str, base: str | None = None):
        if reference.startswith("#"):
            if base is None:
                raise AssertionError(f"relative reference without base: {reference}")
            resource_id, fragment = base, reference[1:]
        else:
            resource_id, marker, fragment = reference.partition("#")
            fragment = fragment if marker else ""
        try:
            value = self.resources[resource_id]
        except KeyError as error:
            raise AssertionError(f"unknown schema resource {resource_id}") from error
        if fragment:
            if not fragment.startswith("/"):
                raise AssertionError(f"unsupported non-pointer fragment {fragment}")
            for token in fragment[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[token]
        return value, resource_id

    def _validate(self, schema, value, path: str, base: str) -> None:
        if "$id" in schema:
            base = schema["$id"]
        if "$ref" in schema:
            target, target_base = self.resolve(schema["$ref"], base)
            self._validate(target, value, path, target_base)
            return
        if "oneOf" in schema:
            matches = 0
            for candidate in schema["oneOf"]:
                try:
                    self._validate(candidate, value, path, base)
                except AssertionError:
                    continue
                matches += 1
            if matches != 1:
                raise AssertionError(
                    f"{path}: expected exactly one schema match, got {matches}"
                )
            return
        if "const" in schema and value != schema["const"]:
            raise AssertionError(f"{path}: expected constant {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            raise AssertionError(f"{path}: {value!r} is outside enum")
        expected = schema.get("type")
        if expected is not None:
            allowed = expected if isinstance(expected, list) else [expected]
            if not any(self._is_type(value, item) for item in allowed):
                raise AssertionError(
                    f"{path}: expected {allowed}, got {type(value).__name__}"
                )
        if isinstance(value, dict):
            required = set(schema.get("required", ()))
            missing = required - value.keys()
            if missing:
                raise AssertionError(f"{path}: missing {sorted(missing)}")
            for key, dependencies in schema.get("dependentRequired", {}).items():
                if key in value:
                    missing_dependencies = set(dependencies) - value.keys()
                    if missing_dependencies:
                        raise AssertionError(
                            f"{path}: {key} requires {sorted(missing_dependencies)}"
                        )
            properties = schema.get("properties", {})
            for key, item in value.items():
                if key in properties:
                    self._validate(properties[key], item, f"{path}.{key}", base)
                else:
                    additional = schema.get("additionalProperties", True)
                    if additional is False:
                        raise AssertionError(f"{path}: unknown field {key}")
                    if isinstance(additional, dict):
                        self._validate(additional, item, f"{path}.{key}", base)
            if len(value) < schema.get("minProperties", 0):
                raise AssertionError(f"{path}: too few properties")
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
                "maxItems", 2**31
            ):
                raise AssertionError(f"{path}: invalid item count")
            if schema.get("uniqueItems") and len(
                {json.dumps(item, sort_keys=True) for item in value}
            ) != len(value):
                raise AssertionError(f"{path}: items are not unique")
            if "contains" in schema:
                matches = 0
                for index, item in enumerate(value):
                    try:
                        self._validate(
                            schema["contains"], item, f"{path}[{index}]", base
                        )
                    except AssertionError:
                        continue
                    matches += 1
                if matches < schema.get("minContains", 1) or matches > schema.get(
                    "maxContains", 2**31
                ):
                    raise AssertionError(f"{path}: contains match count is invalid")
            prefix = schema.get("prefixItems", ())
            for index, child in enumerate(prefix):
                if index < len(value):
                    self._validate(child, value[index], f"{path}[{index}]", base)
            items = schema.get("items")
            if isinstance(items, dict):
                for index, item in enumerate(value[len(prefix) :], start=len(prefix)):
                    self._validate(items, item, f"{path}[{index}]", base)
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                raise AssertionError(f"{path}: string is too short")
            if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
                raise AssertionError(f"{path}: string does not match pattern")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < schema.get("minimum", value):
                raise AssertionError(f"{path}: number is too small")
            if value > schema.get("maximum", value):
                raise AssertionError(f"{path}: number is too large")

    @staticmethod
    def _is_type(value, expected: str) -> bool:
        return {
            "null": value is None,
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "string": isinstance(value, str),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }[expected]


def wire(value):
    return json.loads(json.dumps(value, sort_keys=True))


class CatalogStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()

    def test_index_is_exact_and_every_resource_is_versioned(self) -> None:
        catalog = self.schemas.catalog
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(
            catalog["dialect"], "https://json-schema.org/draft/2020-12/schema"
        )
        indexed_files = {item["file"] for item in catalog["schemas"]}
        self.assertEqual(indexed_files, set(self.schemas.documents))
        indexed_ids = {
            identifier
            for item in catalog["schemas"]
            for identifier in (item["root_id"], *item["public_ids"])
        }
        self.assertEqual(indexed_ids, set(self.schemas.catalog_resources))
        self.assertTrue(
            all(
                identifier.startswith("urn:literate-ai:schema:v1:")
                for identifier in indexed_ids
            )
        )
        for item in catalog["schemas"]:
            self.assertEqual(
                self.schemas.documents[item["file"]]["$id"], item["root_id"]
            )

    def test_every_reference_resolves_and_record_shapes_are_closed(self) -> None:
        def walk(value, base=None):
            if isinstance(value, dict):
                base = value.get("$id", base)
                if "$ref" in value:
                    self.schemas.resolve(value["$ref"], base)
                if value.get("type") == "object" and "properties" in value:
                    self.assertIn("additionalProperties", value)
                for child in value.values():
                    walk(child, base)
            elif isinstance(value, list):
                for child in value:
                    walk(child, base)

        for document in self.schemas.documents.values():
            self.assertEqual(
                document["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            walk(document)


if __name__ == "__main__":
    unittest.main()
