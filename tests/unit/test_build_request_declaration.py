"""Focused contract tests for pre-source build authority."""

from __future__ import annotations

import dataclasses
import unittest

from literate_ai.security import (
    BuildRequest,
    BuildRequestDeclaration,
)
from tests.unit.test_schema_catalog import SchemaCatalog

REVISION = "sha256:" + "a" * 64
SOURCE = "sha256:" + "b" * 64
TOOLCHAIN = "sha256:" + "c" * 64


def declaration() -> BuildRequestDeclaration:
    return BuildRequestDeclaration(
        effective_revision_digest=REVISION,
        builder_id="builder:python-bytecode@1",
        toolchain_digest=TOOLCHAIN,
        sandbox_profile="constrained-host",
        requested_privileges=("compiler", "processes"),
        allowed_outputs=("python-bytecode",),
    )


class BuildRequestDeclarationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = SchemaCatalog()

    def test_is_immutable_canonical_and_round_trips(self) -> None:
        value = declaration()

        self.assertEqual(BuildRequestDeclaration.from_dict(value.to_dict()), value)
        self.assertEqual(
            BuildRequestDeclaration.from_dict(value.to_dict()).identity,
            value.identity,
        )
        self.assertRegex(value.identity, r"^sha256:[0-9a-f]{64}$")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.builder_id = "builder:changed"  # type: ignore[misc]

    def test_wire_contract_rejects_a_source_bundle_digest(self) -> None:
        encoded = declaration().to_dict()
        self.schemas.validate(declaration().SCHEMA, encoded)
        self.schemas.validate("urn:literate-ai:schema:v2:security-contracts", encoded)
        encoded["source_bundle_digest"] = SOURCE

        with self.assertRaisesRegex(ValueError, "fields do not match"):
            BuildRequestDeclaration.from_dict(encoded)
        with self.assertRaisesRegex(AssertionError, "unknown field"):
            self.schemas.validate(declaration().SCHEMA, encoded)

    def test_realize_adds_only_the_exact_source_identity(self) -> None:
        value = declaration()

        realized = value.realize(SOURCE)

        self.assertIsInstance(realized, BuildRequest)
        self.assertEqual(realized.source_bundle_digest, SOURCE)
        self.assertEqual(realized.effective_revision_digest, REVISION)
        self.assertEqual(realized.builder_id, value.builder_id)
        self.assertEqual(realized.toolchain_digest, value.toolchain_digest)
        self.assertEqual(realized.sandbox_profile, value.sandbox_profile)
        self.assertEqual(realized.requested_privileges, value.requested_privileges)
        self.assertEqual(realized.allowed_outputs, value.allowed_outputs)
        self.schemas.validate(realized.SCHEMA, realized.to_dict())

    def test_realize_rejects_a_non_digest_source_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_bundle_digest"):
            declaration().realize("generated/source")


if __name__ == "__main__":
    unittest.main()
