from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from literate_ai.schema_catalog import (
    PUBLISHED_SCHEMA_MANIFEST,
    verify_published_schemas,
)

REPOSITORY = Path(__file__).resolve().parents[2]
SCHEMAS = REPOSITORY / "schemas" / "v1"
GOLDEN = REPOSITORY / "tests" / "fixtures" / "schemas" / "v0.1.1"
GOLDEN_INDEX_SHA256 = "11addba086c94d85370692d1d7817d4d055f48bc55a75fdeb98e559e868297b8"
PUBLISHED_MANIFEST_SHA256 = (
    "21cc0cd2705f4d636b6dbeebee52f53fccd902d8947215a4e87668cc29f78d9a"
)


def _resources(value: object) -> set[str]:
    result: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            identifier = item.get("$id")
            if isinstance(identifier, str):
                result.add(identifier)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return result


class PublishedSchemaImmutabilityTests(unittest.TestCase):
    def test_every_v011_schema_matches_its_frozen_bytes(self) -> None:
        report = verify_published_schemas(SCHEMAS)
        self.assertEqual(
            report,
            {
                "release": "0.1.1",
                "source_tag": "v0.1.1",
                "source_commit": "465260cb450dc1c386e257f93ff0277a28f8666b",
                "algorithm": "sha256",
                "file_count": 17,
            },
        )

    def test_digest_catalog_matches_the_exact_historical_public_index(self) -> None:
        index_bytes = (GOLDEN / "index.json").read_bytes()
        self.assertEqual(hashlib.sha256(index_bytes).hexdigest(), GOLDEN_INDEX_SHA256)
        index = json.loads(index_bytes)
        manifest_bytes = (SCHEMAS / PUBLISHED_SCHEMA_MANIFEST).read_bytes()
        self.assertEqual(
            hashlib.sha256(manifest_bytes).hexdigest(), PUBLISHED_MANIFEST_SHA256
        )
        manifest = json.loads(manifest_bytes)
        self.assertEqual(
            manifest["catalog_index"],
            {
                "path": "index.json",
                "bytes": len(index_bytes),
                "digest": GOLDEN_INDEX_SHA256,
            },
        )
        self.assertEqual((SCHEMAS / "index.json").read_bytes(), index_bytes)
        entries = {item["path"]: item for item in manifest["files"]}
        indexed = {item["file"]: item for item in index["schemas"]}
        self.assertEqual(set(entries), set(indexed))

        all_resources: set[str] = set()
        for name in sorted(entries):
            with self.subTest(schema=name):
                content = (SCHEMAS / name).read_bytes()
                entry = entries[name]
                self.assertEqual(len(content), entry["bytes"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), entry["digest"])
                document = json.loads(content)
                expected = indexed[name]
                self.assertEqual(document["$id"], expected["root_id"])
                resources = _resources(document)
                self.assertEqual(
                    resources,
                    {expected["root_id"], *expected["public_ids"]},
                )
                self.assertTrue(all_resources.isdisjoint(resources))
                all_resources.update(resources)


if __name__ == "__main__":
    unittest.main()
