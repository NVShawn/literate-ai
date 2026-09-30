"""Canonical identity and strict wire validation tests."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from literate_ai.contracts import (
    ComponentCoordinate,
    ContentIdentity,
    ContractValidationError,
    HashAlgorithm,
    canonical_identity,
    canonical_json_bytes,
)


class CanonicalIdentityTests(unittest.TestCase):
    def test_canonical_fixture_is_stable(self) -> None:
        value = {"b": 2, "a": "é"}
        self.assertEqual(canonical_json_bytes(value), '{"a":"é","b":2}'.encode())
        self.assertEqual(
            canonical_identity(value).digest,
            "06c264c46ad5ada9493abd3aa2383fb205ae99d7d0bad40b03a43bfec8a1b8de",
        )

    def test_canonical_json_rejects_nonportable_values(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "floating-point"):
            canonical_json_bytes({"temperature": 1.5})
        with self.assertRaisesRegex(ContractValidationError, "keys must be strings"):
            canonical_json_bytes({1: "not portable"})

    def test_identity_round_trip_and_unknown_field_rejection(self) -> None:
        identity = ContentIdentity(HashAlgorithm.SHA256, "a" * 64)
        self.assertEqual(ContentIdentity.from_dict(identity.to_dict()), identity)
        invalid = identity.to_dict() | {"cache_path": "/tmp/not-semantic"}
        with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
            ContentIdentity.from_dict(invalid)

    def test_coordinates_are_stable_but_not_content_identities(self) -> None:
        coordinate = ComponentCoordinate.parse("component://example/hello")
        self.assertEqual(coordinate.uri, "component://example/hello")
        self.assertEqual(
            ComponentCoordinate.from_dict(coordinate.to_dict()), coordinate
        )
        with self.assertRaisesRegex(ContractValidationError, "lower-case"):
            ComponentCoordinate("Example", "hello")

    def test_contract_values_are_immutable(self) -> None:
        identity = ContentIdentity(HashAlgorithm.SHA256, "b" * 64)
        with self.assertRaises(FrozenInstanceError):
            identity.digest = "c" * 64  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
