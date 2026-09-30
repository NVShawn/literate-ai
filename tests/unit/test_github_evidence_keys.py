"""Issuer transport cannot follow producer routing or revive stale keys."""

import io
import json
import ssl
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

    def test_invalid_local_policy_refuses_before_network(self):
        cases = [{"lifetime_seconds": value} for value in (0, 3601, True, 1.5)]
        cases += [{"timeout_seconds": value} for value in (0, 61, True, float("nan"))]
        cases += [{"clock": None}, {"tls_context": object()}]
        with patch(
            "literate_ai.adapters.github_evidence_keys.http.client.HTTPSConnection"
        ) as factory:
            for kwargs in cases:
                with self.subTest(kwargs=kwargs), self.assertRaises(EvidenceOidcError):
                    GitHubEvidenceKeyLoader(**kwargs)
            factory.assert_not_called()

    def test_fixed_origin_paths_freshness_and_no_ambient_auth(self):
        responses = self.responses()
        result, connections, factory = self.run_loader(
            responses, clock=Mock(side_effect=[100.5, 101, 102])
        )
        self.assertEqual(
            (result.document, result.fetched_at, result.expires_at),
            (self.keys, 100, 400),
        )
        for call, connection, path in zip(
            factory.call_args_list,
            connections,
            ["/.well-known/openid-configuration", "/.well-known/jwks"],
            strict=True,
        ):
            self.assertEqual(call.args, ("token.actions.githubusercontent.com",))
            context = call.kwargs["context"]
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            connection.request.assert_called_once_with(
                "GET",
                path,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
        self.assertTrue(all(0 < size <= 8192 for r in responses for size in r.sizes))

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

    def test_http_failures_do_not_follow_redirects(self):
        for status in (301, 302, 304, 401, 404, 500):
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(EvidenceOidcError, "response-refused"),
            ):
                self.run_loader(
                    [
                        Response(
                            b"",
                            status=status,
                            headers=[("Location", "https://evil.invalid")],
                        )
                    ]
                )

    def test_response_framing_and_size_refusal(self):
        cases = [
            ([("Content-Type", "text/plain")], b"{}"),
            ([("Content-Type", "application/json")] * 2, b"{}"),
            ([("Content-Encoding", "gzip")], b"{}"),
            ([("Content-Length", "65537")], b"{}"),
            ([("Content-Length", "2")] * 2, b"{}"),
            ([("Content-Length", "03")], b"{}"),
            ([("Content-Length", "3")], b"{}"),
            ([("Transfer-Encoding", "gzip")], b"{}"),
            ([("Transfer-Encoding", "chunked"), ("Content-Length", "2")], b"{}"),
            ([], b"x" * 65537),
            ([], b""),
        ]
        for headers, body in cases:
            if not any(k == "Content-Type" for k, _ in headers):
                headers = [("Content-Type", "application/json")] + headers
            with (
                self.subTest(headers=headers, size=len(body)),
                self.assertRaises(EvidenceOidcError),
            ):
                self.run_loader([Response(body, headers=headers)])

    def test_chunked_and_exact_length_are_supported(self):
        for headers in [
            [("Transfer-Encoding", "chunked")],
            [("Content-Length", str(len(self.keys)))],
        ]:
            responses = self.responses()
            responses[1] = Response(
                self.keys, headers=[("Content-Type", "application/json")] + headers
            )
            self.assertEqual(self.run_loader(responses)[0].document, self.keys)

    def test_all_keys_validated_before_snapshot_is_returned(self):
        cases = [
            b'{"keys":[],"keys":[]}',
            b'{"keys":[]}',
            json.dumps({"keys": [self.jwk, self.jwk]}).encode(),
            json.dumps(
                {"keys": [self.jwk, {**self.jwk, "kid": "weak", "kty": "oct"}]}
            ).encode(),
            json.dumps({"keys": [self.jwk] * 33}).encode(),
        ]
        for body in cases:
            with self.subTest(body=body[:30]), self.assertRaises(EvidenceOidcError):
                self.run_loader(self.responses(keys=body))

    def test_fetch_time_cannot_extend_or_roll_back_authority(self):
        for times in ([100, 99], [100, 400], [100, 101, 100], [100, 101, 400]):
            with (
                self.subTest(times=times),
                self.assertRaisesRegex(EvidenceOidcError, "fetch-expired"),
            ):
                self.run_loader(self.responses(), clock=Mock(side_effect=times))
        for value in (True, -1, float("nan"), float("inf"), 2**63):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(EvidenceOidcError, "clock-invalid"),
            ):
                GitHubEvidenceKeyLoader(clock=lambda value=value: value).load()

    def test_tls_checks_cover_mutated_caller_context(self):
        context = ssl.create_default_context()
        loader = GitHubEvidenceKeyLoader(tls_context=context)
        context.check_hostname = False
        with patch(
            "literate_ai.adapters.github_evidence_keys.http.client.HTTPSConnection"
        ) as factory:
            with self.assertRaisesRegex(EvidenceOidcError, "tls-invalid"):
                loader.load()
            factory.assert_not_called()
        context.verify_mode = ssl.CERT_NONE
        with self.assertRaisesRegex(EvidenceOidcError, "tls-invalid"):
            GitHubEvidenceKeyLoader(tls_context=context)

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
