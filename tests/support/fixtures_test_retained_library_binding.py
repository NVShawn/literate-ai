"""Shared fixtures extracted from ``tests.unit.test_retained_library_binding``."""

import copy
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.contracts import canonical_identity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_libraries import RetainedLibraryBinding
from literate_ai.schema_catalog import verify_schema_catalog
from tests.support import fixtures_test_retained_library_exports as export_fixtures
from tests.support import fixtures_test_schema_catalog as schema_fixtures


class RetainedLibraryBindingTests(unittest.TestCase):
    def fixture(self):
        exports = export_fixtures.RetainedLibraryExportTests().fixture()
        keys = sorted(
            (
                export.identity
                for manifest in exports.graph.manifests
                for export in manifest.exports
            ),
            key=lambda item: item.uri,
        )
        return RetainedLibraryBinding(
            "importer",
            "reviewed-artifacts",
            exports,
            BlobRef("a" * 64, 1024, media_type="application/zip"),
            canonical_identity("qualification"),
            canonical_identity("run"),
            canonical_identity("verifier"),
            canonical_identity("policy"),
            BlobRef("b" * 64, 500, media_type="application/json"),
            tuple((key, f"_build/libs/p{index}") for index, key in enumerate(keys)),
        )

    def test_production_catalog_resolves_retained_library_references(self):
        root = Path(__file__).resolve().parents[2] / "schemas" / "v2"
        verify_schema_catalog("v2", root)

    def test_roundtrip_and_schema_keep_native_dependency_destinations(self):
        binding = self.fixture()
        self.assertEqual(len(binding.destinations), 3)
        self.assertEqual(len(binding.exports.libraries), 2)
        self.assertEqual(RetainedLibraryBinding.from_dict(binding.to_dict()), binding)
        schema_fixtures.SchemaCatalog().validate(binding.SCHEMA, binding.to_dict())

    def test_missing_duplicate_reordered_or_foreign_exports_refuse(self):
        binding = self.fixture()
        for destinations in (
            (),
            binding.destinations[:-1],
            binding.destinations[::-1],
            binding.destinations + binding.destinations[:1],
            ((canonical_identity("foreign"), "_build/libs/foreign"),)
            + binding.destinations[1:],
        ):
            with self.subTest(destinations=destinations), self.assertRaises(ValueError):
                replace(binding, destinations=destinations)

    def test_aliases_nested_destinations_and_unsafe_paths_refuse(self):
        binding = self.fixture()
        for paths in (
            ("libs/A", "libs/a", "libs/c"),
            ("libs/a", "libs/a/child", "libs/c"),
            ("../escape", "libs/b", "libs/c"),
            ("C:/escape", "libs/b", "libs/c"),
            ("CON", "libs/b", "libs/c"),
            ("libs/a.", "libs/b", "libs/c"),
            ("libs/a\\b", "libs/b", "libs/c"),
        ):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                replace(
                    binding,
                    destinations=tuple(
                        (row[0], path)
                        for row, path in zip(binding.destinations, paths, strict=True)
                    ),
                )

    def test_no_accepted_flag_ambient_url_or_wrong_media_is_admitted(self):
        binding = self.fixture()
        payload = {**binding.to_dict(), "accepted": True}
        with self.assertRaises(ValueError):
            RetainedLibraryBinding.from_dict(payload)
        for source in ("https://host/token", "/ambient/cache", ""):
            with self.subTest(source=source), self.assertRaises(ValueError):
                replace(binding, source_store_id=source)
        with self.assertRaises(ValueError):
            replace(
                binding,
                qualification_archive=replace(
                    binding.qualification_archive, media_type="application/json"
                ),
            )
        with self.assertRaises(ValueError):
            replace(binding, workspace_plan=replace(binding.workspace_plan, size=0))
        payload = copy.deepcopy(binding.to_dict())
        payload["destinations"][0]["trusted"] = True
        with self.assertRaises(ValueError):
            RetainedLibraryBinding.from_dict(payload)

    def test_changed_reviewed_inputs_change_binding_not_provider_exports(self):
        binding = self.fixture()
        for changes in (
            {"importer_project_id": "another-importer"},
            {"source_store_id": "another-store"},
            {"workspace_plan": replace(binding.workspace_plan, digest="c" * 64)},
            {"policy_identity": canonical_identity("another-policy")},
            {"qualification_identity": canonical_identity("another-qualification")},
        ):
            changed = replace(binding, **changes)
            self.assertNotEqual(changed.identity, binding.identity)
            self.assertEqual(changed.exports, binding.exports)


if __name__ == "__main__":
    unittest.main()
