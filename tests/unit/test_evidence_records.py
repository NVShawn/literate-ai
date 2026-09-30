"""Signed run assertions retain exact subjects and fail closed before admission."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity
from literate_ai.schema_catalog import schema_path, verify_schema_catalog
from literate_ai.security.evidence import (
    DSSE_MEDIA_TYPE,
    STATEMENT_MEDIA_TYPE,
    DerivationRun,
    DsseEnvelope,
    DsseError,
    Ed25519EvidenceSigner,
    EvidenceArtifact,
    EvidenceLocator,
    EvidenceMatrix,
    EvidenceRunContext,
    EvidenceStatement,
    EvidenceStatementError,
    PlatformRun,
    verify_evidence_statement,
)


def _blob(label: str, media_type: str = "application/json") -> BlobRef:
    content = label.encode()
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )


def _records():
    context = EvidenceRunContext(
        "run-123",
        "https://example.test/team/project",
        "a" * 40,
        canonical_identity("workflow"),
        canonical_identity("linux-arm64"),
        100,
        200,
    )
    derivation = DerivationRun(
        context,
        _blob("sources"),
        (EvidenceArtifact("specification", _blob("spec")),),
        _blob("journal"),
        "passed",
    )
    platform = PlatformRun(
        context,
        _blob("receipt"),
        _blob("derivation", DSSE_MEDIA_TYPE),
        _blob("environment"),
        (EvidenceArtifact("acceptance", _blob("result")),),
        "passed",
    )
    matrix = EvidenceMatrix(
        context,
        _blob("matrix-plan"),
        ("linux", "windows"),
        (
            EvidenceArtifact("linux", _blob("linux", DSSE_MEDIA_TYPE)),
            EvidenceArtifact("windows", _blob("windows", DSSE_MEDIA_TYPE)),
        ),
    )
    locator = EvidenceLocator("retained-ci", _blob("receipt"), 300)
    return derivation, platform, matrix, locator


class EvidenceRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(
            schema_path("evidence.schema.json", catalog_version="v2").read_bytes()
        )
        Draft202012Validator.check_schema(cls.document)
        registry = Registry().with_resource(
            cls.document["$id"], Resource.from_contents(cls.document)
        )
        cls.validator = Draft202012Validator(cls.document, registry=registry)

    def test_all_four_predicates_have_public_valid_round_trips(self):
        verify_schema_catalog("v2")
        compatibility = json.loads(
            schema_path("compatibility.json", catalog_version="v2").read_bytes()
        )
        for record in _records():
            with self.subTest(schema=record.SCHEMA):
                self.assertIn(record.SCHEMA, compatibility["write_contracts"])
                self.validator.validate(record.to_dict())
                self.assertEqual(type(record).from_dict(record.to_dict()), record)
                statement = EvidenceStatement(record)
                self.assertEqual(
                    EvidenceStatement.from_bytes(statement.to_bytes()), statement
                )

    def test_independent_signature_verification_precedes_single_exact_parse(self):
        signer = Ed25519EvidenceSigner(bytes(range(32)))
        for record in _records():
            with self.subTest(schema=record.SCHEMA):
                statement = EvidenceStatement(record)
                # Noncanonical formatting is legitimate signed JSON. Never replace
                # these bytes with the parser's canonical serialization for custody.
                raw = json.dumps(statement.to_dict(), indent=2).encode()
                envelope = signer.sign(STATEMENT_MEDIA_TYPE, raw)
                with patch.object(
                    EvidenceStatement, "from_bytes", wraps=EvidenceStatement.from_bytes
                ) as parser:
                    result = verify_evidence_statement(
                        envelope, trusted_public_keys=(signer.public_key,)
                    )
                parser.assert_called_once_with(raw)
                self.assertIs(result.payload, raw)
                self.assertEqual(result.statement, statement)
                self.assertEqual(result.signer_key_identities, (signer.key_identity,))
                self.assertNotEqual(raw, result.statement.to_bytes())

    def test_invalid_signature_and_media_never_reach_predicate_parser(self):
        signer = Ed25519EvidenceSigner(bytes(range(32)))
        signed = signer.sign(
            STATEMENT_MEDIA_TYPE, EvidenceStatement(_records()[0]).to_bytes()
        )
        for envelope in (
            replace(signed, payload=b"secret invalid JSON"),
            signer.sign("text/plain", signed.payload),
            DsseEnvelope(signed.payload_type, signed.payload, ()),
        ):
            with self.subTest(envelope=envelope.payload_type):
                with patch.object(EvidenceStatement, "from_bytes") as parser:
                    with self.assertRaises(DsseError):
                        verify_evidence_statement(
                            envelope, trusted_public_keys=(signer.public_key,)
                        )
                    parser.assert_not_called()

    def test_standard_extensions_are_ignored_but_closed_predicates_refuse_them(self):
        statement = EvidenceStatement(_records()[0])
        wire = statement.to_dict()
        wire["vendor_extension"] = {"value": 1.5}
        wire["subject"][0]["vendor_extension"] = "ignored"
        self.assertEqual(
            EvidenceStatement.from_bytes(json.dumps(wire).encode()), statement
        )
        wire["predicate"]["future_authority"] = True
        with self.assertRaises(EvidenceStatementError):
            EvidenceStatement.from_bytes(json.dumps(wire).encode())

    def test_valid_signature_cannot_hide_substituted_subject_or_predicate_type(self):
        signer = Ed25519EvidenceSigner(bytes(range(32)))
        original = EvidenceStatement(_records()[0]).to_dict()
        for mutation in ("digest", "missing", "extra", "predicate-type", "version"):
            wire = copy.deepcopy(original)
            if mutation == "digest":
                wire["subject"][0]["digest"]["sha256"] = "b" * 64
            elif mutation == "missing":
                wire["subject"] = []
            elif mutation == "extra":
                wire["subject"].append(copy.deepcopy(wire["subject"][0]))
            elif mutation == "predicate-type":
                wire["predicateType"] = PlatformRun.SCHEMA
            else:
                wire["_type"] = "https://in-toto.io/Statement/v2"
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(EvidenceStatementError),
            ):
                verify_evidence_statement(
                    signer.sign(STATEMENT_MEDIA_TYPE, json.dumps(wire).encode()),
                    trusted_public_keys=(signer.public_key,),
                )

    def test_context_omissions_unknown_fields_and_wrong_types_are_refused(self):
        original = _records()[0].to_dict()
        cases = []
        for field in original["context"]:
            wire = copy.deepcopy(original)
            del wire["context"][field]
            cases.append(wire)
        for field, value in (
            ("revision", "main"),
            ("revision", "A" * 40),
            ("started_at", True),
            ("finished_at", -1),
            ("workflow", "unbound"),
            ("invocation_id", "../run"),
            ("repository", " secret"),
        ):
            wire = copy.deepcopy(original)
            wire["context"][field] = value
            cases.append(wire)
        wire = copy.deepcopy(original)
        wire["context"]["trusted"] = True
        cases.append(wire)
        for wire in cases:
            with self.subTest(context=wire["context"]):
                with self.assertRaises(ValueError):
                    DerivationRun.from_dict(wire)
                with self.assertRaises(ValidationError):
                    self.validator.validate(wire)

    def test_time_order_and_matrix_coverage_are_semantically_checked(self):
        derivation, _, matrix, _ = _records()
        with self.assertRaises(ValueError):
            replace(derivation.context, finished_at=99)
        for required, cells in (
            (matrix.required_cells, matrix.cells[:1]),
            (("linux",), matrix.cells),
            (("linux", "linux"), (matrix.cells[0], matrix.cells[0])),
            (tuple(reversed(matrix.required_cells)), tuple(reversed(matrix.cells))),
        ):
            with self.subTest(required=required), self.assertRaises(ValueError):
                replace(matrix, required_cells=required, cells=cells)

    def test_artifacts_bind_size_media_unique_names_and_immutable_collections(self):
        derivation, platform, matrix, locator = _records()
        for changes in (
            {"inputs": list(derivation.inputs)},
            {"inputs": ()},
            {"inputs": derivation.inputs * 2},
            {"inputs": derivation.inputs * 1025},
            {"subject": replace(derivation.subject, size=2**63)},
            {
                "subject": replace(
                    derivation.subject, media_type="application/json; charset=utf-8"
                )
            },
            {"status": "skipped"},
        ):
            with self.subTest(changes=tuple(changes)), self.assertRaises(ValueError):
                replace(derivation, **changes)
        with self.assertRaises(ValueError):
            replace(
                platform,
                derivation=replace(platform.derivation, media_type="text/plain"),
            )
        with self.assertRaises(ValueError):
            replace(matrix, cells=(EvidenceArtifact("linux", _blob("not-envelope")),))
        with self.assertRaises(ValueError):
            replace(locator, store_id="https://user:secret@example.test")
        with self.assertRaises(FrozenInstanceError):
            locator.store_id = "different"
        wire = derivation.to_dict()
        restored = DerivationRun.from_dict(wire)
        wire["inputs"].clear()
        self.assertEqual(restored, derivation)

    def test_failed_and_incomplete_runs_remain_reportable_without_promotion(self):
        for record in _records()[:2]:
            for status in ("failed", "incomplete"):
                value = replace(record, status=status)
                self.validator.validate(value.to_dict())
                self.assertEqual(type(value).from_dict(value.to_dict()), value)

    def test_every_predicate_refuses_unknown_fields_and_invalid_blob_metadata(self):
        for record in _records():
            for field, value in (
                ("future_authority", True),
                ("size", True),
                ("media_type", "Application/JSON"),
                ("digest", "a" * 63),
            ):
                wire = record.to_dict()
                if field == "future_authority":
                    wire[field] = value
                else:
                    wire["subject"][field] = value
                with self.subTest(schema=record.SCHEMA, field=field):
                    with self.assertRaises(ValueError):
                        type(record).from_dict(wire)
                    with self.assertRaises(ValidationError):
                        self.validator.validate(wire)

    def test_statement_verifier_preserves_distinct_signer_threshold(self):
        first = Ed25519EvidenceSigner(bytes(range(32)))
        second = Ed25519EvidenceSigner(bytes(reversed(range(32))))
        raw = EvidenceStatement(_records()[0]).to_bytes()
        signed = first.sign(STATEMENT_MEDIA_TYPE, raw)
        keys = (first.public_key, second.public_key)
        with self.assertRaises(DsseError):
            verify_evidence_statement(
                signed, trusted_public_keys=keys, minimum_signatures=2
            )
        joined = replace(
            signed,
            signatures=signed.signatures
            + second.sign(STATEMENT_MEDIA_TYPE, raw).signatures,
        )
        result = verify_evidence_statement(
            joined, trusted_public_keys=keys, minimum_signatures=2
        )
        self.assertEqual(
            result.signer_key_identities,
            tuple(sorted((first.key_identity, second.key_identity))),
        )

    def test_schema_and_typed_decoder_both_refuse_trailing_newlines(self):
        original = _records()[0].to_dict()
        for field in ("invocation_id", "repository", "revision"):
            wire = copy.deepcopy(original)
            wire["context"][field] += "\n"
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    DerivationRun.from_dict(wire)
                with self.assertRaises(ValidationError):
                    self.validator.validate(wire)
        wire = copy.deepcopy(original)
        wire["subject"]["media_type"] += "\n"
        with self.assertRaises(ValueError):
            DerivationRun.from_dict(wire)
        with self.assertRaises(ValidationError):
            self.validator.validate(wire)

    def test_malformed_payload_errors_do_not_echo_signed_secret_content(self):
        for raw in (
            b'"secret"',
            b'{"secret":NaN}',
            b'{"secret":1,"secret":2}',
            b"[" * 2000,
            b"\xff",
            b'{"secret":' + b"9" * 5000 + b"}",
            b"x" * (8 * 1024 * 1024 + 1),
        ):
            with (
                self.subTest(size=len(raw)),
                self.assertRaises(EvidenceStatementError) as caught,
            ):
                EvidenceStatement.from_bytes(raw)
            self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
