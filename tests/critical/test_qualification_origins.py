"""Authored provenance cannot masquerade as retained source history."""

import copy
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.qualification_origins import (
    QUALIFICATION_ORIGIN_SCHEMA,
    AuthoredQualificationOrigin,
    SourceDerivedQualificationOrigin,
    qualification_origin_from_dict,
)


def identity(label):
    return canonical_identity({"fixture": label})


class QualificationOriginTests(unittest.TestCase):
    def setUp(self):
        self.authored = AuthoredQualificationOrigin(
            identity("project"),
            identity("locked-authority"),
            identity("lock"),
            identity("authored-inputs"),
        )
        self.derived = SourceDerivedQualificationOrigin(
            identity("projection"),
            identity("source-snapshot"),
            identity("generation-audit"),
            identity("promotion-tree"),
            identity("promotion-audit"),
        )

    def test_authored_wire_contains_only_actual_authored_references(self):
        self.assertEqual(
            self.authored.to_dict(),
            {
                "schema": "urn:literate-ai:schema:v2:authored-qualification-origin",
                "origin": "authored",
                "project_configuration_identity": identity("project").to_dict(),
                "locked_generation_authority_identity": identity(
                    "locked-authority"
                ).to_dict(),
                "component_lock_identity": identity("lock").to_dict(),
                "authored_input_closure_identity": identity(
                    "authored-inputs"
                ).to_dict(),
            },
        )
        self.assertEqual(
            qualification_origin_from_dict(self.authored.to_dict()), self.authored
        )
        self.assertEqual(
            qualification_origin_from_dict(self.derived.to_dict()), self.derived
        )
        # The content reference hashes the exact origin-specific, versioned wire.
        self.assertEqual(
            self.authored.identity, canonical_identity(self.authored.to_dict())
        )
        self.assertNotEqual(self.authored.identity, self.derived.identity)

    def test_discriminator_and_schema_cannot_reinterpret_another_origin(self):
        for origin, other in (
            (self.authored, self.derived),
            (self.derived, self.authored),
        ):
            for changes in (
                {"origin": other.KIND},
                {"schema": other.SCHEMA},
                {"schema": other.SCHEMA, "origin": other.KIND},
                {"schema": QUALIFICATION_ORIGIN_SCHEMA},
                {"schema": "urn:literate-ai:schema:v99:qualification-origin"},
            ):
                with self.subTest(origin=origin.KIND, changes=changes):
                    with self.assertRaises(ContractValidationError):
                        qualification_origin_from_dict({**origin.to_dict(), **changes})

    def test_missing_mixed_or_extra_authority_fields_refuse(self):
        for origin, other in (
            (self.authored, self.derived),
            (self.derived, self.authored),
        ):
            wire = origin.to_dict()
            for name in wire:
                candidate = {key: value for key, value in wire.items() if key != name}
                with self.subTest(origin=origin.KIND, missing=name):
                    with self.assertRaises(ContractValidationError):
                        qualification_origin_from_dict(candidate)
            for name, value in other.to_dict().items():
                if name not in wire:
                    with self.subTest(origin=origin.KIND, mixed=name):
                        with self.assertRaises(ContractValidationError):
                            qualification_origin_from_dict({**wire, name: value})
            for name in ("authorized", "qualified", "reviewed", "force_authored"):
                with self.assertRaises(ContractValidationError):
                    qualification_origin_from_dict({**wire, name: True})

    def test_untyped_or_placeholder_identities_are_not_coerced(self):
        for origin in (self.authored, self.derived):
            for name in origin.to_dict():
                if name in {"schema", "origin"}:
                    continue
                for invalid in (None, "", identity("untyped").uri, {}, True):
                    with self.subTest(origin=origin.KIND, field=name, invalid=invalid):
                        with self.assertRaises(ContractValidationError):
                            replace(origin, **{name: invalid})
                        with self.assertRaises(ContractValidationError):
                            qualification_origin_from_dict(
                                {**origin.to_dict(), name: invalid}
                            )

    def test_every_bound_reference_changes_the_origin_identity(self):
        for origin in (self.authored, self.derived):
            for name in origin.to_dict():
                if name not in {"schema", "origin"}:
                    with self.subTest(origin=origin.KIND, field=name):
                        changed = replace(origin, **{name: identity("substitution")})
                        self.assertNotEqual(origin.identity, changed.identity)
        # Returned wire containers do not provide a mutation path into the DTO.
        before = canonical_json_bytes(self.authored.to_dict())
        wire = self.authored.to_dict()
        wire["component_lock_identity"]["digest"] = "0" * 64
        self.assertEqual(before, canonical_json_bytes(self.authored.to_dict()))

    def test_published_schema_agrees_on_valid_and_confused_origins(self):
        import json

        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        from literate_ai.schema_catalog import verify_schema_catalog

        root = Path(__file__).parents[2] / "schemas"
        report = verify_schema_catalog("v2", root / "v2")
        self.assertIsInstance(report, dict)
        resources = []
        for catalog in ("v1", "v2"):
            for path in (root / catalog).glob("*.schema.json"):
                document = json.loads(path.read_bytes())
                resources.append((document["$id"], Resource.from_contents(document)))
        registry = Registry().with_resources(resources)
        document = json.loads(
            (root / "v2/qualification-origin.schema.json").read_bytes()
        )
        validator = Draft202012Validator(document, registry=registry)
        for origin, other in (
            (self.authored, self.derived),
            (self.derived, self.authored),
        ):
            wire = origin.to_dict()
            validator.validate(wire)
            candidates = [
                {**wire, "origin": other.KIND},
                {**wire, "schema": other.SCHEMA},
                {**wire, "qualified": True},
                {
                    **wire,
                    next(key for key in wire if key not in {"schema", "origin"}): None,
                },
            ]
            candidate = copy.deepcopy(wire)
            candidate[next(key for key in wire if key not in {"schema", "origin"})][
                "digest"
            ] = "not-a-digest"
            candidates.append(candidate)
            for candidate in candidates:
                with self.subTest(origin=origin.KIND, candidate=candidate):
                    self.assertTrue(list(validator.iter_errors(candidate)))
                    with self.assertRaises(ContractValidationError):
                        qualification_origin_from_dict(candidate)
