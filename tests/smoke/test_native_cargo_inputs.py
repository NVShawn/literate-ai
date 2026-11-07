"""Exact provider-blob/contract admission controls; fake tool fixture only."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.lifecycle.native_cargo_inputs import bind_native_cargo_inputs
from literate_ai.contracts import LibraryConsumerBinding, canonical_identity
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderGraphError,
)
from tests.smoke import test_standard_cargo_lifecycle as cargo_fixture


class NativeCargoInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-cargo-inputs-")
        self.addCleanup(self.temp.cleanup)
        self.ports, plan, _ = cargo_fixture.StandardCargoLifecycleTests()._system(
            Path(self.temp.name), library=True
        )
        self.artifact = self.ports.build(plan, ()).exports[0]
        self.contract = self.ports.contracts[self.artifact.component_revision.uri]
        surface = self.contract.library_import_surface
        capability = surface.capabilities[0]
        self.consumer = canonical_identity({"native-cargo-inputs": "consumer"})
        self.binding = LibraryConsumerBinding(
            consumer_component_revision=self.consumer,
            provider_component_revision=self.artifact.component_revision,
            capability=capability.capability,
            interface_identity=capability.interface_identity,
            artifact_identity=self.artifact.identity,
            import_surface_identity=surface.identity,
            target_identity=self.artifact.target_identity,
            dependency_artifact_identities=self.artifact.dependency_artifact_identities,
        )

    def bind(self, **changes):
        args = dict(
            consumer_revision=self.consumer,
            target_identity=self.artifact.target_identity,
            providers=(self.artifact,),
            available_exports=self.ports._exports_by_identity,
            contracts=self.ports.contracts,
            manifest_paths={self.artifact.component_revision.uri: "source/Cargo.toml"},
            current_bindings=(self.binding,),
            read_blob=self.ports.read_artifact_blob,
        )
        args.update(changes)
        return bind_native_cargo_inputs(**args)

    def test_exact_export_contract_blob_and_library_binding_join(self):
        graph, sources = self.bind()
        self.assertEqual(graph.nodes[0].artifact_identity, self.artifact.identity)
        self.assertEqual(sources[0].node, graph.nodes[0])
        self.assertEqual(graph.edges[0].consumer_revision, self.consumer)

    def test_missing_current_export_or_target_refused_before_blob_read(self):
        def no_read(_):
            self.fail("refused provider must not read a blob")

        with self.assertRaisesRegex(NativeCargoProviderGraphError, "current_export"):
            self.bind(available_exports={}, read_blob=no_read)
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "export_contract"):
            self.bind(
                target_identity=canonical_identity({"wrong": "target"}),
                read_blob=no_read,
            )

    def test_substituted_binding_fields_refused(self):
        wrong = canonical_identity({"wrong": "binding"})
        for field in (
            "consumer_component_revision",
            "provider_component_revision",
            "artifact_identity",
            "import_surface_identity",
            "target_identity",
            "interface_identity",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    NativeCargoProviderGraphError, "current_bindings"
                ):
                    self.bind(
                        current_bindings=(replace(self.binding, **{field: wrong}),)
                    )
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "current_bindings"):
            self.bind(current_bindings=())

    def test_modified_blob_and_manifest_target_refused(self):
        content = self.ports.read_artifact_blob(self.artifact.blob)
        with self.assertRaises(ValueError):
            self.bind(read_blob=lambda _: content[:-1] + bytes([content[-1] ^ 1]))
        with self.assertRaises(NativeCargoProviderGraphError):
            self.bind(
                manifest_paths={self.artifact.component_revision.uri: "../Cargo.toml"}
            )
