"""Schema validation facts never replace fresh catalogs or reference registries."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from literate_ai import schema_catalog as catalog

ROOT = Path(__file__).resolve().parents[2] / "schemas"
DRAFT = "https://json-schema.org/draft/2020-12/schema"


class SchemaCheckScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        catalog._validated_catalog_root.cache_clear()
        self.addCleanup(catalog._validated_catalog_root.cache_clear)

    def verify_documents(self, *documents):
        catalog._verify_schema_documents(
            catalog_version="v1",
            catalog_root=self.root,
            documents=tuple(
                (self.root / f"{index}.schema.json", document)
                for index, document in enumerate(documents)
            ),
        )

    def test_complete_root_checks_each_schema_value_once(self):
        values = {
            json.dumps(json.loads(path.read_bytes()), sort_keys=True)
            for version in ("v1", "v2")
            for path in (ROOT / version).glob("*.schema.json")
        }
        original = Draft202012Validator.check_schema
        with (
            mock.patch.dict(
                os.environ, {catalog.SCHEMA_CATALOG_ROOT_ENVIRONMENT: str(ROOT)}
            ),
            mock.patch.object(
                Draft202012Validator, "check_schema", wraps=original
            ) as check,
        ):
            self.assertEqual(catalog.schema_catalog_root("v1"), ROOT / "v1")
        self.assertEqual(check.call_count, len(values))
        self.assertIsNone(catalog._SCHEMA_CHECK_FACTS.get())

    def test_independent_verifications_do_not_reuse_success(self):
        original = Draft202012Validator.check_schema
        expected = len(list((ROOT / "v1").glob("*.schema.json")))
        with mock.patch.object(
            Draft202012Validator, "check_schema", wraps=original
        ) as check:
            first = catalog.verify_schema_catalog("v1", ROOT / "v1")
            second = catalog.verify_schema_catalog("v1", ROOT / "v1")
        self.assertEqual(first, second)
        self.assertEqual(check.call_count, 2 * expected)
        self.assertIsNone(catalog._SCHEMA_CHECK_FACTS.get())

    def test_mutating_a_previously_valid_value_still_refuses(self):
        document = {"$schema": DRAFT, "$id": "urn:example:changing", "type": "object"}
        with catalog._schema_check_scope():
            self.verify_documents(document)
            document["type"] = 42
            with self.assertRaises(catalog.SchemaCatalogError) as error:
                self.verify_documents(document)
        self.assertEqual(error.exception.code, "schema.catalog_file_invalid")

    def test_warm_catalog_refuses_same_size_and_mtime_schema_mutation(self):
        copied = self.root / "schemas"
        shutil.copytree(ROOT, copied)
        with mock.patch.dict(
            os.environ, {catalog.SCHEMA_CATALOG_ROOT_ENVIRONMENT: str(copied)}
        ):
            self.assertEqual(catalog.schema_catalog_root("v2"), copied / "v2")
            target = next(
                path
                for path in (copied / "v2").glob("*.schema.json")
                if b'"type": "object"' in path.read_bytes()
            )
            before = target.stat()
            raw = target.read_bytes()
            changed = raw.replace(b'"type": "object"', b'"type": 12345678', 1)
            self.assertEqual(len(changed), len(raw))
            target.write_bytes(changed)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaises(catalog.SchemaCatalogError) as error:
                catalog.schema_catalog_root("v2")
        self.assertEqual(error.exception.code, "schema.catalog_file_invalid")
        self.assertEqual(target.read_bytes(), changed)
        self.assertIsNone(catalog._SCHEMA_CHECK_FACTS.get())

    def test_reference_registry_is_rebuilt_after_a_validation_hit(self):
        source = {
            "$schema": DRAFT,
            "$id": "urn:example:source",
            "$ref": "urn:example:target",
        }
        target = {"$schema": DRAFT, "$id": "urn:example:target", "type": "object"}
        with catalog._schema_check_scope():
            self.verify_documents(source, target)
            with self.assertRaises(catalog.SchemaCatalogError) as error:
                self.verify_documents(source)
        self.assertEqual(error.exception.code, "schema.catalog_reference_unresolved")

    def test_failure_closes_the_scope_before_the_next_operation(self):
        document = {"$schema": DRAFT, "$id": "urn:example:failure", "type": "object"}
        original = Draft202012Validator.check_schema
        with mock.patch.object(
            Draft202012Validator, "check_schema", wraps=original
        ) as check:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with catalog._schema_check_scope():
                    self.verify_documents(document)
                    raise RuntimeError("stop")
            self.assertIsNone(catalog._SCHEMA_CHECK_FACTS.get())
            with catalog._schema_check_scope():
                self.verify_documents(document)
        self.assertEqual(check.call_count, 2)

    def test_full_fact_budget_keeps_validating_uncached_documents(self):
        first = {"$schema": DRAFT, "$id": "urn:example:first", "type": "object"}
        second = {"$schema": DRAFT, "$id": "urn:example:second", "type": "object"}
        original = Draft202012Validator.check_schema
        with (
            mock.patch.object(catalog, "_MAXIMUM_SCHEMA_CHECK_FACTS", 1),
            mock.patch.object(
                Draft202012Validator, "check_schema", wraps=original
            ) as check,
            catalog._schema_check_scope(),
        ):
            self.verify_documents(first, second)
            self.verify_documents(first, second)
            self.assertEqual(len(catalog._SCHEMA_CHECK_FACTS.get()), 1)
        self.assertEqual(check.call_count, 3)
