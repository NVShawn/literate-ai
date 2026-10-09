"""Typed compiled-provider byte custody; native ABI checks belong to conformance."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.builders.mix_project import (
    MixProviderApplication,
    MixProviderLibrary,
    _provider_code_paths,
    _provider_directory,
)
from literate_ai.adapters.builders.python import BuildError, canonical_tree_digest
from literate_ai.application.library_artifacts import project_library_import_surface
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.library_imports import AuthoredLibraryImport


def _surface(symbols=("add",)):
    return project_library_import_surface(
        "provider-api",
        "elixir",
        (("math.add", canonical_identity("fixture-interface")),),
        declarations=(
            AuthoredLibraryImport(
                "elixir", "provider_api", "math.add", "ProviderApi.Math", symbols
            ),
        ),
    )


class MixProviderLibraryTests(unittest.TestCase):
    def test_portable_layout_preserves_distinct_slots_and_legacy_history(self):
        root = Path("retained")
        identity = canonical_identity("first provider")
        other = canonical_identity("second provider")
        self.assertEqual(_provider_directory(root, identity, 0), root / "providers/0")
        self.assertNotEqual(
            _provider_directory(root, identity, 0),
            _provider_directory(root, other, 1),
        )
        self.assertEqual(
            _provider_directory(root, identity, 0, layout="digest-v1"),
            root / "providers" / identity.digest,
        )
        with self.assertRaises(ValueError):
            _provider_directory(root, identity, 0, layout="unknown")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ebin = self.root / "ebin"
        self.ebin.mkdir()
        (self.ebin / "Elixir.ProviderApi.Math.beam").write_bytes(b"fixture bytes")
        self.provider = MixProviderLibrary(
            canonical_identity("fixture-artifact"),
            _surface(),
            self.ebin,
            canonical_tree_digest(self.ebin),
        )

    def test_byte_pin_survives_relocation_and_detects_mutation(self):
        self.provider.require_unchanged()
        relocated = self.root / "relocated"
        shutil.copytree(self.ebin, relocated)
        replace(self.provider, ebin=relocated).require_unchanged()
        self.assertEqual(
            self.provider.to_dict()["import_surface"], _surface().to_dict()
        )
        (self.ebin / "Elixir.ProviderApi.Math.beam").write_bytes(b"changed bytes")
        with self.assertRaises(BuildError):
            self.provider.require_unchanged()

    def test_extra_non_beam_input_is_rejected(self):
        (self.ebin / "provider_api.app").write_bytes(b"not a module")
        with self.assertRaises(BuildError):
            self.provider.require_unchanged()

    def test_nested_module_inputs_are_rejected(self):
        nested = self.ebin / "nested"
        nested.mkdir()
        (nested / "Elixir.ProviderApi.Helper.beam").write_bytes(b"nested")
        with self.assertRaises(BuildError):
            self.provider.require_unchanged()

    def test_linked_module_input_is_rejected(self):
        target = self.root / "external.beam"
        target.write_bytes(b"external")
        try:
            (self.ebin / "Elixir.ProviderApi.Helper.beam").symlink_to(target)
        except OSError:
            self.skipTest("host does not permit symlink creation")
        with self.assertRaises(BuildError):
            self.provider.require_unchanged()

    def test_relative_path_cannot_become_selected_input(self):
        with self.assertRaises(TypeError):
            MixProviderLibrary(
                self.provider.artifact_identity,
                _surface(),
                Path("ebin"),
                self.provider.tree_digest,
            )

    def application_provider(self):
        (self.ebin / "provider_api.app").write_bytes(b"application metadata")
        library = MixProviderApplication(
            "provider_api", "1.0.0", self.ebin, canonical_tree_digest(self.ebin)
        )
        dependency = self.root / "decimal"
        dependency.mkdir()
        (dependency / "decimal.app").write_bytes(b"dependency metadata")
        (dependency / "Elixir.Decimal.beam").write_bytes(b"dependency bytes")
        decimal = MixProviderApplication(
            "decimal", "2.3.0", dependency, canonical_tree_digest(dependency)
        )
        return replace(
            self.provider,
            tree_digest=library.tree_digest,
            applications=(library, decimal),
        )

    def test_application_metadata_and_dependency_bytes_are_pinned(self):
        provider = self.application_provider()
        provider.require_unchanged()
        self.assertEqual(len(provider.to_dict()["applications"]), 2)
        self.assertEqual(
            _provider_code_paths((provider,)), (self.ebin, self.root / "decimal")
        )
        (self.root / "decimal/Elixir.Decimal.beam").write_bytes(b"changed")
        with self.assertRaises(BuildError):
            provider.require_unchanged()

    def test_application_cannot_claim_another_library_or_extra_metadata(self):
        provider = self.application_provider()
        with self.assertRaises(ValueError):
            replace(provider, applications=tuple(reversed(provider.applications)))
        (self.ebin / "other.app").write_bytes(b"foreign app")
        with self.assertRaises(BuildError):
            provider.require_unchanged()

    def test_conflicting_application_closures_refuse_code_paths(self):
        provider = self.application_provider()
        other = replace(
            provider,
            artifact_identity=canonical_identity("other-provider"),
            applications=(
                provider.applications[0],
                replace(provider.applications[1], version="2.4.0"),
            ),
        )
        with self.assertRaises(BuildError) as conflict:
            _provider_code_paths((provider, other))
        self.assertEqual(conflict.exception.code, "builder.mix_provider_conflict")
        self.assertEqual(
            _provider_code_paths((provider, provider)), provider.code_paths
        )
