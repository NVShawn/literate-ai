"""Versioned wire contracts for explicit Standard release authority and evidence."""

from __future__ import annotations

import hashlib
import unittest

from literate_ai.application import (
    ReleaseArtifactAssemblyError,
    StandardReleaseDeclaration,
)
from literate_ai.contracts import BlobRef, canonical_identity, canonical_json_bytes
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.executable_components import (
    PackageEntrypoint,
    PackageFileKind,
    PackageInput,
    PackageKind,
    RuntimeRequirement,
    RuntimeRequirementKind,
)
from literate_ai.publication import StandardProjectReleaseReceipt
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def _identity(label: str):
    return canonical_identity({"standard-release-wire": label})


class StandardReleaseWireContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root_artifact = _identity("root-artifact")
        content = b"resource"
        resource = PackageInput(
            "share/config.json",
            "configuration",
            PackageFileKind.RESOURCE,
            _identity("resource-source"),
            _identity("target"),
            BlobRef(
                digest=hashlib.sha256(content).hexdigest(),
                size=len(content),
                media_type="application/json",
            ),
        )
        self.declaration = StandardReleaseDeclaration(
            _identity("execution-plan"),
            _identity("component-lock"),
            _identity("root-revision"),
            _identity("target"),
            self.root_artifact,
            PackageKind.RUNTIME_BUNDLE,
            _identity("packager"),
            ((self.root_artifact.uri, "bin/app"),),
            (PackageEntrypoint("app", "application", "bin/app", self.root_artifact),),
            (resource,),
            (
                RuntimeRequirement(
                    "runtime",
                    RuntimeRequirementKind.INTERPRETER,
                    "Portable runtime",
                    _identity("runtime"),
                    True,
                ),
            ),
        )
        release_identities = tuple(
            sorted(
                (self.declaration.identity, _identity("second-declaration")),
                key=lambda item: item.uri,
            )
        )
        self.receipt = StandardProjectReleaseReceipt(
            component_lock_identity=self.declaration.component_lock_identity,
            execution_plan_identity=self.declaration.execution_plan_identity,
            lifecycle_receipt_identity=_identity("lifecycle-receipt"),
            artifact_graph_identity=_identity("artifact-graph"),
            release_declaration_identities=release_identities,
            release_artifact_set_identity=_identity("release-set"),
            publication_request_identity=_identity("publication-request"),
            transfer_receipt_identity=_identity("transfer-receipt"),
            root_component_revision=self.declaration.root_component_revision,
            target_identity=self.declaration.target_identity,
        )

    def test_declaration_round_trips_with_stable_identity(self) -> None:
        encoded = self.declaration.to_dict()
        decoded = StandardReleaseDeclaration.from_dict(encoded)

        self.assertEqual(decoded, self.declaration)
        self.assertEqual(decoded.identity, self.declaration.identity)
        self.assertEqual(
            canonical_json_bytes(decoded.to_dict()), canonical_json_bytes(encoded)
        )
        SchemaCatalog().validate(StandardReleaseDeclaration.SCHEMA, encoded)

    def test_receipt_round_trips_with_stable_identity(self) -> None:
        encoded = self.receipt.to_dict()
        decoded = StandardProjectReleaseReceipt.from_dict(encoded)

        self.assertEqual(decoded, self.receipt)
        self.assertEqual(decoded.identity, self.receipt.identity)
        self.assertEqual(
            canonical_json_bytes(decoded.to_dict()), canonical_json_bytes(encoded)
        )
        SchemaCatalog().validate(StandardProjectReleaseReceipt.SCHEMA, encoded)

    def test_contracts_reject_unknown_fields_and_schema_drift(self) -> None:
        declaration = self.declaration.to_dict()
        declaration["ambient_override"] = True
        with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
            StandardReleaseDeclaration.from_dict(declaration)

        receipt = self.receipt.to_dict()
        receipt["schema"] = "urn:literate-ai:schema:v3:standard-project-release-receipt"
        with self.assertRaisesRegex(ContractValidationError, "must be"):
            StandardProjectReleaseReceipt.from_dict(receipt)

        declaration = self.declaration.to_dict()
        declaration["entrypoints"] = []
        with self.assertRaisesRegex(
            ReleaseArtifactAssemblyError, "entrypoints must be typed"
        ):
            StandardReleaseDeclaration.from_dict(declaration)

    def test_receipt_requires_canonical_declaration_identity_order(self) -> None:
        encoded = self.receipt.to_dict()
        encoded["release_declaration_identities"] = list(
            reversed(encoded["release_declaration_identities"])
        )
        with self.assertRaisesRegex(ValueError, "unique, and sorted"):
            StandardProjectReleaseReceipt.from_dict(encoded)


if __name__ == "__main__":
    unittest.main()
