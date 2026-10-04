"""Signed run assertions retain exact subjects and fail closed before admission."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity
from literate_ai.schema_catalog import schema_path
from literate_ai.security.evidence import (
    DSSE_MEDIA_TYPE,
    STATEMENT_MEDIA_TYPE,
    DerivationRun,
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
