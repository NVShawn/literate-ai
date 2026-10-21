"""Tree-aware SDK product authority and refusal of mixed wire capabilities."""

from __future__ import annotations

import dataclasses
import hashlib
import unittest

from literate_ai.adapters.native_sdk_build import NativeSdkRepositoryBuilder
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.native_sdk_tree_products import (
    NativeSdkTreeBuildLayout,
    NativeSdkTreeBuildRecipe,
    NativeSdkTreeSnapshot,
)
from literate_ai.contracts.native_sdk_trees import NativeSdkAlias, NativeSdkTree
from literate_ai.contracts.native_sdks import (
    NativeSdkBuildLayout,
    NativeSdkBuildRecipe,
    NativeSdkFile,
    NativeSdkSnapshot,
)
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_project import RetainedProjectLimits
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def _file(path: str, value: bytes) -> NativeSdkFile:
    return NativeSdkFile(path, BlobRef(hashlib.sha256(value).hexdigest(), len(value)))


class NativeSdkTreeProductTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = NativeSdkTree(
            ("bin", "include", "lib"),
            (
                _file("bin/CC", b"compiler"),
                _file("include/api.h", b"header"),
                _file("lib/sdk.so.1", b"library"),
            ),
            (
                NativeSdkAlias("bin/cc", "bin/CC", "hardlink"),
                NativeSdkAlias("headers", "include"),
                NativeSdkAlias("lib/sdk.so", "sdk.so.1"),
            ),
            case_sensitive=True,
        )
        self.surface = LibraryImportSurface(
            "c",
            "sdk",
            (
                LibraryCapabilityImport(
                    "sdk.api",
                    canonical_identity("interface"),
                    "api.h",
                    ("sdk_version",),
                ),
            ),
        )
        self.snapshot = NativeSdkTreeSnapshot(
            canonical_identity("source"),
            canonical_identity("recipe"),
            canonical_identity("target"),
            canonical_identity("license"),
            self.surface,
            "headers",
            ("lib/sdk.so",),
            self.tree,
        )
        self.layout = NativeSdkTreeBuildLayout(
            "out/sdk",
            "headers",
            self.surface,
            ("lib/sdk.so",),
            "LICENSE",
            "LICENSE",
            canonical_identity("license"),
            "freebsd",
            "x86_64",
            RetainedProjectLimits(100, 1024, 4096),
            True,
        )

    def test_roundtrip_schema_and_logical_file_bindings(self) -> None:
        for value in (self.snapshot, self.layout):
            with self.subTest(schema=value.SCHEMA):
                self.assertEqual(type(value).from_dict(value.to_dict()), value)
                SchemaCatalog().validate(value.SCHEMA, value.to_dict())
        self.assertEqual(self.snapshot.file_at("lib/sdk.so").path, "lib/sdk.so.1")
        self.assertEqual(self.snapshot.file_at("bin/cc").path, "bin/CC")
        self.assertEqual(self.snapshot.files, self.tree.files)

    def test_import_root_requires_a_nonempty_directory(self) -> None:
        empty = dataclasses.replace(
            self.tree, directories=("bin", "empty", "include", "lib")
        )
        for root in ("lib/sdk.so", "missing", "empty"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                dataclasses.replace(self.snapshot, tree=empty, import_root=root)

    def test_library_paths_require_resolved_regular_files(self) -> None:
        for paths in (
            ("headers",),
            ("missing",),
            ("lib/sdk.so/child",),
            ("lib/sdk.so", "lib/sdk.so"),
        ):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                dataclasses.replace(self.snapshot, native_libraries=paths)

    def test_input_and_alias_changes_invalidate_snapshot_identity(self) -> None:
        for name in (
            "source_lock_identity",
            "recipe_identity",
            "target_identity",
            "license_identity",
        ):
            with self.subTest(name=name):
                changed = dataclasses.replace(
                    self.snapshot, **{name: canonical_identity("changed")}
                )
                self.assertNotEqual(changed.identity, self.snapshot.identity)
        changed = dataclasses.replace(
            self.tree,
            aliases=(
                self.tree.aliases[0],
                NativeSdkAlias("headers", "./include"),
                self.tree.aliases[2],
            ),
        )
        self.assertNotEqual(
            dataclasses.replace(self.snapshot, tree=changed).identity,
            self.snapshot.identity,
        )

    def test_layout_binds_limits_target_and_case_policy(self) -> None:
        for values in (
            {"case_sensitive": False},
            {"architecture": "arm64"},
            {"limits": dataclasses.replace(self.layout.limits, max_total_bytes=8192)},
        ):
            with self.subTest(values=values):
                self.assertNotEqual(
                    dataclasses.replace(self.layout, **values).identity,
                    self.layout.identity,
                )
        self.assertEqual(
            dataclasses.replace(
                self.layout, native_libraries=("bin/CC", "bin/cc")
            ).case_sensitive,
            True,
        )
        with self.assertRaises(ValueError):
            dataclasses.replace(
                self.layout, native_libraries=("bin/CC", "bin/cc"), case_sensitive=False
            )

    def test_legacy_decoders_and_recipes_do_not_enable_tree_products(self) -> None:
        for decoder, wire in (
            (NativeSdkSnapshot, self.snapshot.to_dict()),
            (NativeSdkBuildLayout, self.layout.to_dict()),
        ):
            with self.subTest(decoder=decoder), self.assertRaises(ValueError):
                decoder.from_dict(wire)
        with self.assertRaisesRegex(ValueError, "typed layout"):
            NativeSdkBuildRecipe("sdk", self.layout, (), ())
        legacy = NativeSdkSnapshot(
            self.snapshot.source_lock_identity,
            self.snapshot.recipe_identity,
            self.snapshot.target_identity,
            self.snapshot.license_identity,
            self.surface,
            "include",
            ("lib/sdk.so.1",),
            self.tree.files,
        )
        self.assertEqual(NativeSdkSnapshot.from_dict(legacy.to_dict()), legacy)
        self.assertEqual(
            legacy.to_dict()["schema"], "urn:literate-ai:schema:v2:native-sdk-snapshot"
        )
        with self.assertRaises(ValueError):
            NativeSdkTreeSnapshot.from_dict(legacy.to_dict())

    def test_repository_builder_refuses_untyped_layout_before_execution_resources(
        self,
    ) -> None:
        with self.assertRaisesRegex(TypeError, "supported SDK layout"):
            NativeSdkRepositoryBuilder(
                layout=object(),
                tools={},
                store=None,
                grant_lookup=lambda identity: None,
                environment={},
            )

    def test_unknown_admission_claims_and_invalid_limits_refuse(self) -> None:
        for value in (self.snapshot, self.layout):
            with self.subTest(schema=value.SCHEMA), self.assertRaises(ValueError):
                type(value).from_dict({**value.to_dict(), "admitted": True})
        for values in ({"case_sensitive": 1}, {"limits": None}):
            with self.subTest(values=values), self.assertRaises(TypeError):
                dataclasses.replace(self.layout, **values)

    def test_batch_resolution_preserves_logical_order_and_refuses_bad_members(
        self,
    ) -> None:
        self.assertEqual(
            self.tree.resolve_all(("headers/api.h", "lib/sdk.so", "bin/cc")),
            ("include/api.h", "lib/sdk.so.1", "bin/CC"),
        )
        with self.assertRaises(ValueError):
            self.tree.resolve_all(("headers/api.h", "lib/sdk.so/child"))

    def test_explicit_tree_recipe_roundtrip_and_mixed_version_refusal(self) -> None:
        recipe = NativeSdkTreeBuildRecipe(
            "sdk",
            self.layout,
            ("cmake",),
            (RepositoryBuildCommand("build", ("cmake", "--build", "_build")),),
        )
        self.assertEqual(NativeSdkTreeBuildRecipe.from_dict(recipe.to_dict()), recipe)
        SchemaCatalog().validate(recipe.SCHEMA, recipe.to_dict())
        with self.assertRaises(ValueError):
            NativeSdkBuildRecipe.from_dict(recipe.to_dict())
        for values in (
            {"tools": ("unbound",)},
            {"commands": recipe.commands * 2},
            {"commands": ()},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                dataclasses.replace(recipe, **values)
