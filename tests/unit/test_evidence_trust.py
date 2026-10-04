"""Policy inputs stay independent of even correctly signed producer assertions."""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from literate_ai.contracts.identity import canonical_identity
from literate_ai.schema_catalog import schema_path, verify_schema_catalog
from literate_ai.security.evidence import (
    STATEMENT_MEDIA_TYPE,
    DsseEnvelope,
    DsseError,
    Ed25519EvidenceSigner,
    EvidenceLocator,
    EvidenceMatrix,
    EvidenceRevocations,
    EvidenceSignerRule,
    EvidenceStatement,
    EvidenceTrustError,
    EvidenceTrustPolicy,
    RunEvidenceExpectation,
    check_retention_evidence,
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

    def test_public_policy_records_have_closed_round_trips_and_stable_identities(self):
        verify_schema_catalog("v2")
        for value in (
            _rule(),
            _policy(),
            _revocations(),
            _expectation(),
            _expectation(_records()[2]),
        ):
            with self.subTest(schema=value.SCHEMA):
                wire = value.to_dict()
                self.validator.validate(wire)
                self.assertEqual(type(value).from_dict(wire), value)
                wire["unexpected"] = "authority"
                with self.assertRaises(ValueError):
                    type(value).from_dict(wire)
                with self.assertRaises(ValidationError):
                    self.validator.validate(wire)
        self.assertEqual(
            _policy().identity,
            EvidenceTrustPolicy.from_dict(_policy().to_dict()).identity,
        )
        self.assertNotEqual(
            _revocations().identity,
            replace(_revocations(), issuers=("revoked",)).identity,
        )
        with self.assertRaises(FrozenInstanceError):
            _policy().minimum_signatures = 0

    def test_valid_runs_keep_exact_payload_and_bind_the_independent_check_inputs(self):
        for record in _records()[:3]:
            expectation = _expectation(record)
            envelope = _envelope(record)
            first = check_run_evidence(
                envelope,
                expectation=expectation,
                policy=_policy(),
                revocations=_revocations(),
                now=210,
            )
            self.assertIs(first.authenticated.payload, envelope.payload)
            self.assertEqual(first.policy_identity, _policy().identity)
            self.assertEqual(first.revocations_identity, _revocations().identity)
            self.assertEqual(
                first.expectation_identity, canonical_identity(expectation.to_dict())
            )
            self.assertEqual(
                first.qualified_signer_key_identities, (_signer().key_identity,)
            )
            self.assertEqual(
                first,
                check_run_evidence(
                    envelope,
                    expectation=expectation,
                    policy=_policy(),
                    revocations=_revocations(),
                    now=210,
                ),
            )

    def test_correct_signatures_cannot_replace_required_context_or_subject_metadata(
        self,
    ):
        original = _records()[0]
        for field, value in (
            ("repository", "another-repository"),
            ("revision", "b" * 40),
            ("workflow", canonical_identity("other-workflow")),
            ("target", canonical_identity("other-platform")),
            ("invocation_id", "replayed-invocation"),
        ):
            record = replace(
                original, context=replace(original.context, **{field: value})
            )
            with (
                self.subTest(field=field),
                self.assertRaises(EvidenceTrustError) as caught,
            ):
                self.check(record)
            self.assertEqual(caught.exception.code, f"evidence.trust.{field}-mismatch")
        for field, value in (
            ("digest", "f" * 64),
            ("size", original.subject.size + 1),
            ("media_type", "text/plain"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(EvidenceTrustError) as caught,
            ):
                self.check(
                    replace(
                        original, subject=replace(original.subject, **{field: value})
                    )
                )
            self.assertEqual(caught.exception.code, "evidence.trust.subject-mismatch")

    def test_a_producer_cannot_reduce_its_own_matrix_requirements(self):
        matrix = _records()[2]
        reduced = replace(matrix, required_cells=("linux",), cells=matrix.cells[:1])
        # The reduced record is structurally valid and correctly signed.
        with self.assertRaises(EvidenceTrustError) as caught:
            self.check(reduced, expectation=_expectation(matrix))
        self.assertEqual(
            caught.exception.code, "evidence.trust.matrix-coverage-mismatch"
        )

    def test_run_windows_freshness_future_time_and_duration_are_independent(self):
        original = _records()[0]
        cases = (
            (
                replace(original.context, started_at=89),
                _policy(),
                "run-window-mismatch",
            ),
            (
                replace(original.context, finished_at=221),
                _policy(),
                "run-window-mismatch",
            ),
            (replace(original.context, finished_at=213), _policy(), "run-in-future"),
            (replace(original.context, finished_at=149), _policy(), "run-stale"),
            (
                original.context,
                replace(_policy(), maximum_run_duration_seconds=99),
                "run-duration-exceeded",
            ),
        )
        for context, policy, code in cases:
            with (
                self.subTest(code=code),
                self.assertRaises(EvidenceTrustError) as caught,
            ):
                self.check(replace(original, context=context), policy=policy)
            self.assertEqual(caught.exception.code, "evidence.trust." + code)
        # Exactly the configured age and duration bounds remain valid.
        self.check(
            replace(original, context=replace(original.context, finished_at=150)),
            policy=replace(_policy(), maximum_run_duration_seconds=50),
        )

    def test_expired_future_or_old_revocation_snapshots_are_not_authenticated(self):
        for snapshot in (
            replace(_revocations(), issued_at=211),
            replace(_revocations(), expires_at=210),
            replace(_revocations(), issued_at=149),
        ):
            with (
                self.subTest(snapshot=snapshot),
                patch(
                    "literate_ai.security.evidence.checks.verify_evidence_statement"
                ) as authenticate,
            ):
                with self.assertRaises(EvidenceTrustError) as caught:
                    self.check(revocations=snapshot)
                self.assertEqual(
                    caught.exception.code, "evidence.trust.revocations-stale"
                )
                authenticate.assert_not_called()

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

    def test_distinct_threshold_counts_only_signers_qualified_for_the_actual_run(self):
        first, second = _signer(), Ed25519EvidenceSigner(bytes(reversed(range(32))))
        record = _records()[0]
        envelope = _envelope(record, first)
        joined = replace(
            envelope,
            signatures=envelope.signatures + _envelope(record, second).signatures,
        )
        policy = _policy(_rule(first), _rule(second), minimum=2)
        settings = dict(
            expectation=_expectation(),
            policy=policy,
            revocations=_revocations(),
            now=210,
        )
        checked = check_run_evidence(joined, **settings)
        self.assertEqual(len(checked.qualified_signer_key_identities), 2)
        with self.assertRaises(DsseError):
            check_run_evidence(
                replace(envelope, signatures=envelope.signatures * 2), **settings
            )
        recent = replace(_rule(second), valid_from=101)
        with self.assertRaises(EvidenceTrustError) as caught:
            check_run_evidence(
                joined,
                **{**settings, "policy": _policy(_rule(first), recent, minimum=2)},
            )
        self.assertEqual(
            caught.exception.code, "evidence.trust.signer-run-window-mismatch"
        )
        checked = check_run_evidence(
            joined, **{**settings, "policy": _policy(_rule(first), recent)}
        )
        self.assertEqual(checked.qualified_signer_key_identities, (first.key_identity,))
        with self.assertRaises(ValueError):
            _policy(
                _rule(first), replace(_rule(first), issuer="issuer-alias"), minimum=2
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

    def test_failed_or_incomplete_runs_and_wrong_predicate_types_do_not_pass(self):
        for record in _records()[:2]:
            for status in ("failed", "incomplete"):
                with (
                    self.subTest(status=status),
                    self.assertRaises(EvidenceTrustError) as caught,
                ):
                    self.check(
                        replace(record, status=status), expectation=_expectation(record)
                    )
                self.assertEqual(caught.exception.code, "evidence.trust.run-not-passed")
        with self.assertRaises(EvidenceTrustError) as caught:
            self.check(_records()[1])
        self.assertEqual(caught.exception.code, "evidence.trust.predicate-mismatch")

    def test_retention_requires_matching_subject_store_authority_and_deadline(self):
        subject = _records()[0].subject
        locator = EvidenceLocator("local", subject, 300)
        settings = dict(
            subject=subject,
            store_id="local",
            expectation=_expectation(),
            policy=_policy(),
            revocations=_revocations(),
            now=210,
        )
        checked = check_retention_evidence(_envelope(locator), **settings)
        self.assertEqual(
            checked.qualified_signer_key_identities, (_signer().key_identity,)
        )
        for changed, code in (
            (replace(locator, retained_until=299), "retention-too-short"),
            (replace(locator, store_id="different"), "retention-subject-mismatch"),
            (
                replace(locator, subject=replace(subject, media_type="text/plain")),
                "retention-subject-mismatch",
            ),
        ):
            with (
                self.subTest(code=code),
                self.assertRaises(EvidenceTrustError) as caught,
            ):
                check_retention_evidence(_envelope(changed), **settings)
            self.assertEqual(caught.exception.code, "evidence.trust." + code)
        with self.assertRaises(EvidenceTrustError):
            check_retention_evidence(
                _envelope(locator), **{**settings, "store_id": "unconfigured"}
            )
        with self.assertRaises(EvidenceTrustError):
            check_retention_evidence(
                _envelope(locator),
                **{
                    **settings,
                    "revocations": replace(_revocations(), issuers=(_rule().issuer,)),
                },
            )

    def test_configuration_rejects_weak_or_mutable_and_noncanonical_inputs(self):
        for changes in (
            {"minimum_signatures": 0},
            {"minimum_signatures": True},
            {"minimum_signatures": 2},
            {"maximum_run_age_seconds": 0},
            {"minimum_retention_seconds": 0},
            {"signers": list(_policy().signers)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(_policy(), **changes)
        for changes in (
            {"repositories": ("repo\n",)},
            {"public_key": bytearray(32)},
            {"store_ids": ()},
            {"predicate_types": ("unknown",)},
            {"valid_until": 0},
            {"repositories": ("z", "a")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(_rule(), **changes)
        with self.assertRaises(ValueError):
            replace(_expectation(), required_cells=("undeclared",))
        with self.assertRaises(ValueError):
            replace(_expectation(_records()[2]), required_cells=())
        wire = _policy().to_dict()
        restored = EvidenceTrustPolicy.from_dict(wire)
        wire["signers"].clear()
        self.assertEqual(restored, _policy())


if __name__ == "__main__":
    unittest.main()
