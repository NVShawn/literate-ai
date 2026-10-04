from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.schema_catalog import (
    PUBLISHED_SCHEMA_MANIFEST,
    PublishedSchemaError,
    verify_published_schemas,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

REPOSITORY = Path(__file__).resolve().parents[2]
SCHEMAS = REPOSITORY / "schemas" / "v1"
GOLDEN = REPOSITORY / "tests" / "fixtures" / "schemas" / "v0.1.1"
DEFERRED_DRIFT = (
    REPOSITORY / "tests" / "fixtures" / "schemas" / "unreleased-v1-drift.json"
)
GOLDEN_INDEX_SHA256 = "11addba086c94d85370692d1d7817d4d055f48bc55a75fdeb98e559e868297b8"
PUBLISHED_MANIFEST_SHA256 = (
    "21cc0cd2705f4d636b6dbeebee52f53fccd902d8947215a4e87668cc29f78d9a"
)
HISTORICAL_RESULT_SHA256 = (
    "8d618a5d09bc4c3d042af18925fea57b39bc364f8914c7ec701a2a3844088c7c"
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

    def test_one_byte_change_fails_under_the_same_published_uri(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "v1"
            shutil.copytree(SCHEMAS, root)
            target = root / "components.schema.json"
            target.write_bytes(target.read_bytes() + b" ")
            with self.assertRaises(PublishedSchemaError) as raised:
                verify_published_schemas(root)
        self.assertEqual(raised.exception.code, "schema.published_file_drift")
        self.assertIn("components.schema.json", str(raised.exception))

    def test_one_byte_catalog_index_change_fails_under_the_same_catalog_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "v1"
            shutil.copytree(SCHEMAS, root)
            target = root / "index.json"
            target.write_bytes(target.read_bytes() + b" ")
            with self.assertRaises(PublishedSchemaError) as raised:
                verify_published_schemas(root)
        self.assertEqual(raised.exception.code, "schema.published_index_drift")

    def test_symlinked_catalog_root_is_rejected_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "real-v1"
            shutil.copytree(SCHEMAS, target)
            link = parent / "linked-v1"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            with self.assertRaises(PublishedSchemaError) as raised:
                verify_published_schemas(link)
        self.assertEqual(raised.exception.code, "schema.catalog_root_invalid")

    def test_historical_source_to_specification_result_validates_without_graph(
        self,
    ) -> None:
        fixture_bytes = (GOLDEN / "source-to-specification-result.json").read_bytes()
        self.assertEqual(
            hashlib.sha256(fixture_bytes).hexdigest(), HISTORICAL_RESULT_SHA256
        )
        document = json.loads(fixture_bytes)
        self.assertNotIn("component_graph_draft", document)
        SchemaCatalog().validate(
            "urn:literate-ai:schema:v1:source-to-specification-result", document
        )
        evidence = json.loads(DEFERRED_DRIFT.read_bytes())
        source_result = next(
            item
            for item in evidence["files"]
            if item["path"] == "source-to-specification.schema.json"
        )
        self.assertEqual(
            source_result["change_classes"],
            ["added-required-component-graph-draft"],
        )

    def test_manifest_omission_and_rebinding_fail_closed(self) -> None:
        mutations = (
            lambda value: value["files"].pop(),
            lambda value: value["files"][0].__setitem__("path", "../escape.json"),
            lambda value: value["files"][0].__setitem__("digest", "0" * 64),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate.__code__.co_firstlineno):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "v1"
                    shutil.copytree(SCHEMAS, root)
                    path = root / PUBLISHED_SCHEMA_MANIFEST
                    value = json.loads(path.read_bytes())
                    mutate(value)
                    path.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaises(PublishedSchemaError):
                        verify_published_schemas(root)

    def test_unreleased_same_uri_drift_remains_explicitly_deferred(self) -> None:
        manifest = json.loads((SCHEMAS / PUBLISHED_SCHEMA_MANIFEST).read_bytes())
        published = {item["path"]: item["digest"] for item in manifest["files"]}
        evidence = json.loads(DEFERRED_DRIFT.read_bytes())
        self.assertEqual(evidence["published_baseline"], "v0.1.1")
        self.assertEqual(
            evidence["status"],
            "deferred-until-new-wire-identities-are-designed",
        )
        self.assertEqual(
            [item["path"] for item in evidence["files"]],
            sorted(item["path"] for item in evidence["files"]),
        )
        for item in evidence["files"]:
            with self.subTest(schema=item["path"]):
                self.assertEqual(published[item["path"]], item["published_digest"])
                self.assertNotEqual(item["published_digest"], item["unreleased_digest"])
                self.assertTrue(item["reused_uris"])
                self.assertIsInstance(item["unreleased_only_uris"], list)
                self.assertTrue(
                    set(item["reused_uris"]).isdisjoint(item["unreleased_only_uris"])
                )
                self.assertTrue(item["change_classes"])


if __name__ == "__main__":
    unittest.main()
