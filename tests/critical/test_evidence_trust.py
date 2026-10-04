"""Policy inputs stay independent of even correctly signed producer assertions."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from literate_ai.contracts.identity import canonical_identity
from literate_ai.schema_catalog import schema_path
from literate_ai.security.evidence import (
    STATEMENT_MEDIA_TYPE,
    DsseEnvelope,
    DsseError,
    Ed25519EvidenceSigner,
    EvidenceMatrix,
    EvidenceRevocations,
    EvidenceSignerRule,
    EvidenceStatement,
    EvidenceTrustError,
    EvidenceTrustPolicy,
    RunEvidenceExpectation,
    check_run_evidence,
)
from literate_ai.security.evidence.records import PREDICATE_TYPES
from tests.support.fixtures_test_evidence_records import _records


def _signer():
    return Ed25519EvidenceSigner(bytes(range(32)))


def _rule(signer=None):
    signer = signer or _signer()
    context = _records()[0].context
    return EvidenceSignerRule(
        signer.public_key,
        "local:self-hosting",
        (context.repository,),
        (context.workflow,),
        (context.target,),
        tuple(sorted(PREDICATE_TYPES)),
        ("local",),
        0,
        1000,
    )


def _policy(*rules, minimum=1):
    return EvidenceTrustPolicy(
        tuple(sorted(rules or (_rule(),), key=lambda rule: rule.key_identity.uri)),
        minimum,
        60,
        300,
        2,
        60,
        90,
    )


def _revocations():
    return EvidenceRevocations(200, 250, (), (), ())


def _expectation(record=None):
    # Derive only the fixed test baseline here. Mutation tests retain this baseline
    # while changing and legitimately re-signing the producer's assertion.
    record = record or _records()[0]
    c = record.context
    return RunEvidenceExpectation(
        record.SCHEMA,
        record.subject,
        c.invocation_id,
        c.repository,
        c.revision,
        c.workflow,
        c.target,
        90,
        220,
        record.required_cells if isinstance(record, EvidenceMatrix) else (),
    )


def _envelope(record, signer=None):
    return (signer or _signer()).sign(
        STATEMENT_MEDIA_TYPE, EvidenceStatement(record).to_bytes()
    )


class EvidenceTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        document = json.loads(
            schema_path("evidence-trust.schema.json", catalog_version="v2").read_bytes()
        )
        Draft202012Validator.check_schema(document)
        registry = Registry().with_resource(
            document["$id"], Resource.from_contents(document)
        )
        cls.validator = Draft202012Validator(document, registry=registry)

    def check(self, record=None, **kwargs):
        settings = dict(
            expectation=_expectation(),
            policy=_policy(),
            revocations=_revocations(),
            now=210,
        )
        settings.update(kwargs)
        return check_run_evidence(_envelope(record or _records()[0]), **settings)

    def test_revocation_and_scope_are_applied_before_authentication(self):
        rule = _rule()
        cases = [
            (_policy(), replace(_revocations(), key_identities=(rule.key_identity,))),
            (_policy(), replace(_revocations(), issuers=(rule.issuer,))),
            (
                _policy(),
                replace(_revocations(), invocation_ids=(_expectation().invocation_id,)),
            ),
        ]
        for changed in (
            replace(rule, repositories=("other",)),
            replace(rule, workflows=(canonical_identity("other"),)),
            replace(rule, targets=(canonical_identity("other"),)),
            replace(rule, valid_from=211),
            replace(rule, valid_until=210),
            replace(rule, predicate_types=(EvidenceMatrix.SCHEMA,), store_ids=()),
        ):
            cases.append((_policy(changed), _revocations()))
        for policy, revocations in cases:
            with (
                self.subTest(policy=policy.identity.uri),
                patch(
                    "literate_ai.security.evidence.checks.verify_evidence_statement"
                ) as authenticate,
            ):
                with self.assertRaises(EvidenceTrustError):
                    self.check(policy=policy, revocations=revocations)
                authenticate.assert_not_called()

    def test_signer_must_be_valid_for_the_run_as_well_as_the_check(self):
        with self.assertRaises(EvidenceTrustError) as caught:
            self.check(policy=_policy(replace(_rule(), valid_from=101)))
        self.assertEqual(
            caught.exception.code, "evidence.trust.signer-run-window-mismatch"
        )

    def test_unsigned_wrong_key_and_forged_key_hints_never_establish_trust(self):
        original = _envelope(_records()[0])
        stranger = _envelope(_records()[0], Ed25519EvidenceSigner(b"x" * 32))
        forged = replace(
            stranger,
            signatures=(
                replace(stranger.signatures[0], key_id=_signer().key_identity),
            ),
        )
        for envelope in (
            DsseEnvelope(original.payload_type, original.payload, ()),
            stranger,
            forged,
        ):
            with (
                self.subTest(signatures=envelope.signatures),
                self.assertRaises(DsseError),
            ):
                check_run_evidence(
                    envelope,
                    expectation=_expectation(),
                    policy=_policy(),
                    revocations=_revocations(),
                    now=210,
                )


if __name__ == "__main__":
    unittest.main()
