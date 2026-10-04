"""A valid issuer token cannot substitute for possession of exact signed evidence."""

import hashlib
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.contracts.blobs import BlobRef
from literate_ai.security.evidence import DSSE_MEDIA_TYPE, Ed25519EvidenceSigner
from literate_ai.security.evidence.github_oidc import verify_github_run_evidence
from tests.support import fixtures_test_github_evidence_oidc as oidc_fixtures
from tests.support.fixtures_test_evidence_records import _blob, _records
from tests.support.fixtures_test_evidence_trust import _envelope, _expectation, _signer


class GitHubRunEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        oidc_fixtures.GitHubEvidenceOidcTests.setUpClass()

    def setUp(self):
        self.fixture = oidc_fixtures.GitHubEvidenceOidcTests()
        self.fixture.setUp()
        self.record = self.github_record(_records()[0])
        self.expectation = _expectation(self.record)

    def github_record(self, record):
        return replace(
            record,
            context=replace(
                record.context, repository="https://github.com/example/project"
            ),
        )

    def arguments(
        self,
        record=None,
        *,
        signer=None,
        public_key=None,
        expectation=None,
        policy=None,
    ):
        envelope = _envelope(record or self.record, signer).to_bytes()
        request = replace(
            self.fixture.request,
            evidence=BlobRef(
                hashlib.sha256(envelope).hexdigest(),
                len(envelope),
                media_type=DSSE_MEDIA_TYPE,
            ),
            expectation=expectation or self.expectation,
            signer_public_key=public_key or _signer().public_key,
        )
        policy = policy or self.fixture.policy
        token = self.fixture.token(
            {**self.fixture.claims, "aud": request.audience(policy)}
        )
        return dict(
            token=token,
            envelope=envelope,
            request=request,
            policy=policy,
            key_set=self.fixture.keys,
            now=210,
        )

    def test_all_run_roles_require_the_exact_signed_payload(self):
        for record in _records()[:3]:
            record = self.github_record(record)
            args = self.arguments(record, expectation=_expectation(record))
            result = verify_github_run_evidence(**args)
            self.assertEqual(result.envelope, args["envelope"])
            self.assertEqual(result.authenticated.statement.predicate, record)
            self.assertEqual(
                result.identity_check.request_identity, args["request"].identity
            )
            self.assertEqual(
                result.identity, verify_github_run_evidence(**args).identity
            )
            self.assertNotIn(args["token"].decode(), repr(result))

    def test_correct_oidc_token_cannot_replace_the_named_private_key(self):
        other = Ed25519EvidenceSigner(bytes(reversed(range(32))))
        for args in (
            self.arguments(signer=other),
            self.arguments(public_key=other.public_key),
        ):
            with (
                self.subTest(key=args["request"].signer_public_key),
                self.assertRaisesRegex(ValueError, "signature"),
            ):
                verify_github_run_evidence(**args)

    def test_changed_envelope_bytes_refuse_before_dsse_verification(self):
        args = self.arguments()
        for envelope in (
            args["envelope"] + b" ",
            bytes([args["envelope"][0] ^ 1]) + args["envelope"][1:],
        ):
            with patch(
                "literate_ai.security.evidence.github_oidc.verify_evidence_statement"
            ) as verifier:
                with self.assertRaises(ValueError):
                    verify_github_run_evidence(**{**args, "envelope": envelope})
                verifier.assert_not_called()

    def test_invalid_token_refuses_before_possession_check(self):
        args = self.arguments()
        with patch(
            "literate_ai.security.evidence.github_oidc.verify_evidence_statement"
        ) as verifier:
            with self.assertRaises(ValueError):
                verify_github_run_evidence(**{**args, "token": self.fixture.token()})
            verifier.assert_not_called()

    def test_legitimately_signed_and_token_bound_wrong_runs_refuse(self):
        changes = [
            replace(self.record, subject=_blob("other")),
            replace(self.record, status="failed"),
        ]
        for name, value in [
            ("invocation_id", "other"),
            ("repository", "https://github.com/example/other"),
            ("revision", "b" * 40),
            ("started_at", 89),
            ("finished_at", 221),
        ]:
            changes.append(
                replace(
                    self.record, context=replace(self.record.context, **{name: value})
                )
            )
        for record in changes:
            with (
                self.subTest(record=record),
                self.assertRaisesRegex(ValueError, "evidence.trust."),
            ):
                verify_github_run_evidence(**self.arguments(record))

    def test_run_age_duration_and_future_limits_apply_independently_of_jwt(self):
        for policy in (
            replace(self.fixture.policy, maximum_run_age_seconds=9),
            replace(self.fixture.policy, maximum_run_duration_seconds=99),
        ):
            with self.assertRaisesRegex(ValueError, "evidence.trust."):
                verify_github_run_evidence(**self.arguments(policy=policy))
        args = self.arguments()
        with self.assertRaisesRegex(ValueError, "run-in-future"):
            verify_github_run_evidence(
                **{
                    **args,
                    "now": 200 - 1,
                    "key_set": replace(self.fixture.keys, fetched_at=190),
                    "token": self.fixture.token(
                        {
                            **self.fixture.claims,
                            "iat": 190,
                            "nbf": 190,
                            "aud": args["request"].audience(args["policy"]),
                        }
                    ),
                }
            )
        self.assertNotEqual(
            self.fixture.policy.identity,
            replace(self.fixture.policy, maximum_run_age_seconds=9).identity,
        )

    def test_matrix_required_cells_are_independent_of_the_signed_matrix(self):
        matrix = self.github_record(_records()[2])
        expectation = replace(_expectation(matrix), required_cells=("unplanned",))
        with self.assertRaisesRegex(ValueError, "matrix-coverage-mismatch"):
            verify_github_run_evidence(
                **self.arguments(matrix, expectation=expectation)
            )

    def test_refuses_nonbytes_and_oversized_envelopes_before_token_checks(self):
        args = self.arguments()
        for envelope in ("{}", b"", b"x" * (16 * 1024 * 1024 + 1)):
            with self.assertRaisesRegex(ValueError, "envelope-invalid"):
                verify_github_run_evidence(**{**args, "envelope": envelope})
