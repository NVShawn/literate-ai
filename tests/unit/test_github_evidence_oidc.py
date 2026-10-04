"""Real issuer JWT signatures never establish evidence identity outside exact scope."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from literate_ai.contracts.identity import canonical_identity
from literate_ai.security.evidence.github_oidc import (
    GITHUB_OIDC_ISSUER,
    EvidenceOidcError,
    GitHubEvidenceIdentityPolicy,
    GitHubEvidenceIdentityRequest,
    GitHubIssuerKeySet,
    verify_github_evidence_identity,
)
from tests.support.fixtures_test_evidence_records import _blob
from tests.support.fixtures_test_evidence_trust import _expectation, _signer


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


if __name__ == "__main__":
    unittest.main()
