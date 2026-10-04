"""Issuer transport cannot follow producer routing or revive stale keys."""

import io
import json
import unittest
from email.message import Message
from unittest.mock import Mock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from literate_ai.adapters.github_evidence_keys import GitHubEvidenceKeyLoader
from literate_ai.security.evidence.github_oidc import (
    GITHUB_OIDC_ISSUER,
    EvidenceOidcError,
)


class Response:
    def __init__(self, body, *, status=200, headers=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = Message()
        for name, value in (
            headers if headers is not None else [("Content-Type", "application/json")]
        ):
            self.headers[name] = value
        self.sizes = []
        self.closed = False

    def read1(self, size):
        self.sizes.append(size)
        return self.body.read(min(size, 1000))

    def close(self):
        self.closed = True


class GitHubEvidenceKeyLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = {
            **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
            "kid": "test",
        }

    def setUp(self):
        self.discovery = {
            "issuer": GITHUB_OIDC_ISSUER,
            "jwks_uri": GITHUB_OIDC_ISSUER + "/.well-known/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        self.keys = json.dumps({"keys": [self.jwk]}).encode()

    def responses(self, discovery=None, keys=None):
        return [
            Response(
                json.dumps(self.discovery if discovery is None else discovery).encode()
            ),
            Response(self.keys if keys is None else keys),
        ]

    def run_loader(self, responses, *, clock=None, **kwargs):
        connections = [Mock() for _ in responses]
        for connection, response in zip(connections, responses, strict=True):
            connection.getresponse.return_value = response
        with patch(
            "literate_ai.adapters.github_evidence_keys.http.client.HTTPSConnection",
            side_effect=connections,
        ) as factory:
            try:
                result = GitHubEvidenceKeyLoader(
                    clock=clock or (lambda: 100), **kwargs
                ).load()
            finally:
                for connection, response in zip(
                    connections[: factory.call_count],
                    responses[: factory.call_count],
                    strict=True,
                ):
                    connection.close.assert_called_once()
                    self.assertTrue(response.closed)
        return result, connections, factory

    def test_discovery_cannot_redirect_key_fetch(self):
        for field, value in [
            ("issuer", "https://evil.invalid"),
            ("jwks_uri", "https://evil.invalid/key"),
            ("jwks_uri", GITHUB_OIDC_ISSUER + "/other"),
            ("id_token_signing_alg_values_supported", ["HS256"]),
        ]:
            with (
                self.subTest(field=field, value=value),
                self.assertRaisesRegex(EvidenceOidcError, "discovery-invalid"),
            ):
                self.run_loader(self.responses({**self.discovery, field: value}))

    def test_transport_failure_is_sanitized_and_never_returns_cached_keys(self):
        loader = GitHubEvidenceKeyLoader(clock=lambda: 100)
        responses = self.responses()
        connection = Mock()
        connection.getresponse.side_effect = responses
        with patch(
            "literate_ai.adapters.github_evidence_keys.http.client.HTTPSConnection",
            return_value=connection,
        ):
            self.assertEqual(loader.load().document, self.keys)
            connection.request.side_effect = OSError("private transport detail")
            with self.assertRaisesRegex(
                EvidenceOidcError, "^evidence.oidc.transport-failed$"
            ):
                loader.load()
        self.assertEqual(connection.close.call_count, 3)
