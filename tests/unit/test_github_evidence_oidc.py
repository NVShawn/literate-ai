"""Real issuer JWT signatures never establish evidence identity outside exact scope."""

from __future__ import annotations

import base64
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from literate_ai.contracts.identity import canonical_identity
from literate_ai.security.evidence.github_oidc import (
    GITHUB_OIDC_ISSUER,
    EvidenceOidcError,
    GitHubEvidenceIdentityPolicy,
    GitHubEvidenceIdentityRequest,
    GitHubIssuerKeySet,
    verify_github_evidence_identity,
)
from tests.unit.test_evidence_records import _blob
from tests.unit.test_evidence_trust import _expectation, _signer


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


class GitHubEvidenceOidcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.expectation = replace(
            _expectation(), repository="https://github.com/example/project"
        )
        self.policy = GitHubEvidenceIdentityPolicy(
            tuple(
                sorted(
                    {
                        "sub": "repo:example/project:ref:refs/heads/main",
                        "repository": "example/project",
                        "repository_id": "123",
                        "repository_owner_id": "456",
                        "workflow_ref": (
                            "example/project/.github/workflows/release.yml@refs/heads/main"
                        ),
                        "workflow_sha": "b" * 40,
                        "event_name": "push",
                        "ref": "refs/heads/main",
                        "runner_environment": "github-hosted",
                    }.items()
                )
            ),
            self.expectation.workflow,
            (self.expectation.target,),
        )
        self.request = GitHubEvidenceIdentityRequest(
            _blob("exact signed envelope", "application/vnd.dsse.envelope.v1+json"),
            _signer().public_key,
            self.expectation,
            "1000",
            "1",
            "2000",
            canonical_identity("fresh challenge"),
        )
        self.jwk = {
            **jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True),
            "kid": "issuer-1",
            "alg": "RS256",
            "use": "sig",
            "key_ops": ["verify"],
        }
        self.keys = GitHubIssuerKeySet(
            json.dumps({"keys": [self.jwk]}).encode(), 200, 500
        )
        self.claims = {
            **dict(self.policy.claims),
            "iss": GITHUB_OIDC_ISSUER,
            "aud": self.request.audience(self.policy),
            "sha": self.expectation.revision,
            "run_id": "1000",
            "run_attempt": "1",
            "check_run_id": "2000",
            "jti": "unique-token",
            "iat": 200,
            "nbf": 200,
            "exp": 400,
        }

    def token(self, claims=None, *, key=None, headers=None):
        return jwt.encode(
            self.claims if claims is None else claims,
            key or self.key,
            algorithm="RS256",
            headers=headers or {"kid": "issuer-1"},
        ).encode()

    def verify(self, token=None, **kwargs):
        arguments = dict(
            request=self.request, policy=self.policy, key_set=self.keys, now=210
        )
        arguments.update(kwargs)
        return verify_github_evidence_identity(
            self.token() if token is None else token, **arguments
        )

    def refuses(self, code, token=None, **kwargs):
        with self.assertRaises(EvidenceOidcError) as caught:
            self.verify(token, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_valid_live_identity_binds_policy_request_keys_and_exact_token(self):
        token = self.token()
        result = self.verify(token)
        self.assertEqual(result.request_identity, self.request.identity)
        self.assertEqual(result.policy_identity, self.policy.identity)
        self.assertEqual(result.key_set_identity, self.keys.identity)
        self.assertEqual(
            (result.checked_at, result.issued_at, result.expires_at), (210, 200, 400)
        )
        self.assertEqual(self.verify(token), result)
        self.assertNotIn(token.decode(), repr(result))
        with self.assertRaises(FrozenInstanceError):
            result.checked_at = 999

    def test_correctly_signed_claim_substitutions_fail(self):
        for name in (
            *dict(self.policy.claims),
            "sha",
            "run_id",
            "run_attempt",
            "check_run_id",
        ):
            with self.subTest(name=name):
                self.refuses(
                    "evidence.oidc.claims-mismatch",
                    self.token({**self.claims, name: "other"}),
                )
        for name in ("iss", "aud"):
            self.refuses(
                "evidence.oidc.token-invalid",
                self.token({**self.claims, name: "other"}),
            )
        self.refuses(
            "evidence.oidc.token-invalid",
            self.token({**self.claims, "aud": [self.claims["aud"]]}),
        )

    def test_custom_audience_binds_key_evidence_challenge_and_each_run_attempt(self):
        for request in (
            replace(self.request, signer_public_key=b"x" * 32),
            replace(
                self.request,
                evidence=_blob("different envelope", self.request.evidence.media_type),
            ),
            replace(self.request, challenge=canonical_identity("different challenge")),
            replace(self.request, run_attempt="2"),
            replace(self.request, check_run_id="2001"),
            replace(
                self.request,
                expectation=replace(self.expectation, invocation_id="other"),
            ),
        ):
            with self.subTest(identity=request.identity):
                self.refuses("evidence.oidc.token-invalid", request=request)
        self.refuses(
            "evidence.oidc.token-invalid",
            policy=replace(self.policy, maximum_token_age_seconds=100),
        )

    def test_request_cannot_use_other_repository_workflow_or_target(self):
        for field, value in (
            ("repository", "https://github.com/other/project"),
            ("workflow", canonical_identity("other")),
            ("target", canonical_identity("other")),
        ):
            request = replace(
                self.request, expectation=replace(self.expectation, **{field: value})
            )
            self.refuses("evidence.oidc.request-scope-mismatch", request=request)

    def test_signature_algorithm_and_token_key_hints_never_change_trust(self):
        self.refuses("evidence.oidc.token-invalid", self.token(key=self.stranger))
        for header in (
            {"kid": "issuer-1", "jku": "https://attacker.invalid/keys"},
            {"kid": "issuer-1", "crit": ["custom"]},
            {"kid": "issuer-1", "jwk": self.jwk},
            {"kid": "issuer-1", "typ": "different"},
        ):
            self.refuses("evidence.oidc.header-invalid", self.token(headers=header))
        hmac_token = jwt.encode(
            self.claims, b"x" * 64, algorithm="HS256", headers={"kid": "issuer-1"}
        ).encode()
        self.refuses("evidence.oidc.header-invalid", hmac_token)
        self.refuses(
            "evidence.oidc.key-unknown", self.token(headers={"kid": "missing"})
        )

    def test_jwks_age_expiry_ambiguity_and_bad_key_use_fail_closed(self):
        with patch("literate_ai.security.evidence.github_oidc.jwt.decode") as decode:
            for keys in (
                replace(self.keys, fetched_at=211),
                replace(self.keys, expires_at=210),
                replace(self.keys, fetched_at=0),
            ):
                policy = replace(self.policy, maximum_key_set_age_seconds=100)
                self.refuses("evidence.oidc.keys-stale", key_set=keys, policy=policy)
            decode.assert_not_called()
        self.refuses(
            "evidence.oidc.keys-ambiguous",
            key_set=replace(
                self.keys, document=json.dumps({"keys": [self.jwk, self.jwk]}).encode()
            ),
        )
        for field, value in (
            ("alg", "HS256"),
            ("use", "enc"),
            ("key_ops", ["sign"]),
            ("d", "private"),
        ):
            self.refuses(
                "evidence.oidc.key-invalid",
                key_set=replace(
                    self.keys,
                    document=json.dumps(
                        {"keys": [{**self.jwk, field: value}]}
                    ).encode(),
                ),
            )

    def test_expiry_future_age_lifetime_and_integer_time_constraints(self):
        for change in (
            {"exp": 210},
            {"iat": 211, "nbf": 211},
            {"nbf": 211},
            {"iat": 201, "nbf": 200},
            {"exp": 1000},
            {"iat": True},
            {"iat": "200"},
            {"exp": 400.0},
            {"nbf": -1},
        ):
            self.refuses(
                "evidence.oidc.time-invalid", self.token({**self.claims, **change})
            )
        policy = replace(self.policy, maximum_token_age_seconds=9)
        claims = {**self.claims, "aud": self.request.audience(policy)}
        self.refuses("evidence.oidc.time-invalid", self.token(claims), policy=policy)
        self.assertEqual(self.verify(now=399).expires_at, 400)

    def test_unexpected_reusable_workflow_or_environment_cannot_bypass_scope(self):
        for name in ("environment", "job_workflow_ref", "job_workflow_sha"):
            self.refuses(
                "evidence.oidc.claims-mismatch",
                self.token({**self.claims, name: "unexpected"}),
            )
        scope = {
            **dict(self.policy.claims),
            "environment": "release",
            "job_workflow_ref": (
                "example/control/.github/workflows/trusted.yml@refs/tags/v1"
            ),
            "job_workflow_sha": "c" * 40,
        }
        policy = replace(self.policy, claims=tuple(sorted(scope.items())))
        claims = {**self.claims, **scope, "aud": self.request.audience(policy)}
        self.assertEqual(
            self.verify(self.token(claims), policy=policy).policy_identity,
            policy.identity,
        )
        del claims["job_workflow_sha"]
        self.refuses("evidence.oidc.claims-mismatch", self.token(claims), policy=policy)

    def test_duplicate_json_and_oversized_tokens_are_refused_before_library_decode(
        self,
    ):
        header = _b64(b'{"alg":"RS256","kid":"issuer-1","typ":"JWT"}')
        payload = _b64(b'{"iss":"first","iss":"second"}')
        signed = header + b"." + payload
        signature = self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        with patch("literate_ai.security.evidence.github_oidc.jwt.decode") as decode:
            self.refuses("evidence.oidc.json-invalid", signed + b"." + _b64(signature))
            self.refuses("evidence.oidc.token-invalid", b"x" * (32768 + 1))
            self.refuses("evidence.oidc.encoding-invalid", self.token() + b"=")
            decode.assert_not_called()

    def test_weak_keys_large_key_sets_and_deep_json_refuse_before_verification(self):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        weak_jwk = {
            **jwt.algorithms.RSAAlgorithm.to_jwk(weak.public_key(), as_dict=True),
            "kid": "issuer-1",
        }
        many = [{**self.jwk, "kid": str(i)} for i in range(33)]
        header = _b64(b'{"alg":"RS256","kid":"issuer-1","typ":"JWT"}')
        payload = _b64(b'{"nested":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}")
        with patch("literate_ai.security.evidence.github_oidc.jwt.decode") as decode:
            self.refuses(
                "evidence.oidc.key-invalid",
                key_set=replace(
                    self.keys, document=json.dumps({"keys": [weak_jwk]}).encode()
                ),
            )
            self.refuses(
                "evidence.oidc.keys-invalid",
                key_set=replace(
                    self.keys, document=json.dumps({"keys": many}).encode()
                ),
            )
            self.refuses(
                "evidence.oidc.json-invalid",
                header + b"." + payload + b"." + _b64(b"x" * 256),
            )
            decode.assert_not_called()

    def test_policy_cannot_omit_immutable_ids_or_accept_mutable_collections(self):
        for changes in (
            {
                "claims": tuple(
                    (k, v) for k, v in self.policy.claims if k != "repository_id"
                )
            },
            {"claims": list(self.policy.claims)},
            {"targets": []},
            {"claims": (*self.policy.claims, self.policy.claims[0])},
        ):
            with self.assertRaises(EvidenceOidcError):
                replace(self.policy, **changes)
        with self.assertRaises(EvidenceOidcError):
            replace(self.request, run_attempt="01")


if __name__ == "__main__":
    unittest.main()
