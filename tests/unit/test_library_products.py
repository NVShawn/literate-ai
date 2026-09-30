"""Library result projection is typed without changing the existing wire shape."""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from literate_ai.contracts import (
    ArtifactExport,
    BlobRef,
    ContractValidationError,
    LibraryArtifactProduct,
    LibraryCapabilityImport,
    LibraryImportSurface,
    canonical_identity,
    canonical_json_bytes,
)


def library_product(language: str = "python") -> LibraryArtifactProduct:
    identities = [canonical_identity({"fixture": index}) for index in range(8)]
    media = "application/octet-stream"
    artifact = ArtifactExport(
        "library",
        identities[0],
        "library",
        identities[1],
        identities[2],
        media,
        identities[3],
        identities[4],
        identities[5],
        identities[6],
        (identities[7],),
        BlobRef("a" * 64, 1, media_type=media),
    )
    surface = LibraryImportSurface(
        language,
        "fixture",
        (LibraryCapabilityImport("fixture.logic", identities[0], "fixture", ("run",)),),
    )
    return LibraryArtifactProduct(artifact, surface)


class LibraryArtifactProductTests(unittest.TestCase):
    def test_rebuild_projection_rejects_wrong_role_or_component_before_command(
        self,
    ) -> None:
        from literate_ai.cli.rebuild import _standard_root_product_result
        from literate_ai.cli.source_to_specification import CliFailure

        product = library_product()
        revision = product.artifact_export.component_revision
        ports = Mock()
        ports.contracts = {
            revision.uri: SimpleNamespace(
                is_library=True, library_import_surface=product.import_surface
            )
        }
        plan = SimpleNamespace(component_revision=revision)
        for export in (
            replace(product.artifact_export, role="executable"),
            replace(
                product.artifact_export, component_revision=canonical_identity("other")
            ),
        ):
            with self.subTest(export=export.identity.uri):
                with self.assertRaises(CliFailure) as raised:
                    _standard_root_product_result(ports, plan, (export,), export)
                self.assertEqual(
                    raised.exception.code, "standard_rebuild.library_artifact_invalid"
                )
        ports.execution_command.assert_not_called()

    def test_round_trip_preserves_existing_rebuild_wire_bytes(self) -> None:
        for language in ("python", "javascript", "rust"):
            with self.subTest(language=language):
                product = library_product(language)
                previous = {
                    "schema": "literate-ai/library-rebuild-artifact@1",
                    "artifact_export": product.artifact_export.to_dict(),
                    "import_surface": product.import_surface.to_dict(),
                }
                self.assertEqual(
                    canonical_json_bytes(product.to_dict()),
                    canonical_json_bytes(previous),
                )
                self.assertEqual(LibraryArtifactProduct.from_dict(previous), product)
                self.assertEqual(product.identity, canonical_identity(previous))

    def test_rejects_executable_and_untyped_authority(self) -> None:
        product = library_product()
        for artifact, surface in (
            (
                replace(product.artifact_export, role="executable"),
                product.import_surface,
            ),
            (product.artifact_export.to_dict(), product.import_surface),
            (product.artifact_export, product.import_surface.to_dict()),
            (None, product.import_surface),
        ):
            with self.subTest(artifact=type(artifact), surface=type(surface)):
                with self.assertRaises(ContractValidationError):
                    LibraryArtifactProduct(artifact, surface)

    def test_rejects_open_or_malformed_envelopes(self) -> None:
        original = library_product().to_dict()
        bad = []
        for field in original:
            value = copy.deepcopy(original)
            del value[field]
            bad.append(value)
        bad.append({**original, "argv": ["fake-entrypoint"]})
        bad.append({**original, "schema": "literate-ai/library-rebuild-artifact@2"})
        value = copy.deepcopy(original)
        value["artifact_export"]["role"] = "executable"
        bad.append(value)
        value = copy.deepcopy(original)
        value["artifact_export"]["blob"]["digest"] = "not-a-digest"
        bad.append(value)
        value = copy.deepcopy(original)
        value["import_surface"]["capabilities"] = []
        bad.append(value)
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    LibraryArtifactProduct.from_dict(value)

    def test_identity_binds_every_export_and_import_authority(self) -> None:
        original = library_product()
        changed = canonical_identity({"changed": True})
        products = []
        for field in (
            "component_revision",
            "abi_identity",
            "target_identity",
            "producer_identity",
            "source_tree_identity",
            "toolchain_identity",
            "authorization_identity",
        ):
            products.append(
                replace(
                    original,
                    artifact_export=replace(
                        original.artifact_export, **{field: changed}
                    ),
                )
            )
        products.append(
            replace(
                original,
                artifact_export=replace(
                    original.artifact_export, dependency_artifact_identities=(changed,)
                ),
            )
        )
        products.append(
            replace(
                original,
                artifact_export=replace(
                    original.artifact_export,
                    blob=replace(original.artifact_export.blob, digest="b" * 64),
                ),
            )
        )
        products.append(
            replace(
                original,
                import_surface=replace(original.import_surface, package="different"),
            )
        )
        capability = original.import_surface.capabilities[0]
        products.append(
            replace(
                original,
                import_surface=replace(
                    original.import_surface,
                    capabilities=(replace(capability, interface_identity=changed),),
                ),
            )
        )
        for product in products:
            self.assertNotEqual(product.identity, original.identity)
            self.assertEqual(
                LibraryArtifactProduct.from_dict(product.to_dict()), product
            )


if __name__ == "__main__":
    unittest.main()
