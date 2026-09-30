"""Fresh GitHub issuer keys through fixed, authenticated HTTPS endpoints."""

from __future__ import annotations

import http.client
import math
import re
import ssl
import time

from literate_ai.security.evidence.github_oidc import (
    GITHUB_OIDC_ISSUER,
    EvidenceOidcError,
    GitHubIssuerKeySet,
    _issuer_key,
    _json,
)

_HOST = "token.actions.githubusercontent.com"
_DISCOVERY = "/.well-known/openid-configuration"
_KEYS = "/.well-known/jwks"
_LIMIT = 64 * 1024


class GitHubEvidenceKeyLoader:
    """No cache, credentials, proxies, redirects or token-directed key discovery.

    The trusted caller owns the clock, TLS roots and freshness policy. Timeout is
    per socket operation; it is not a hard whole-operation execution deadline.
    A snapshot's lifetime starts before discovery, so slow fetches cannot extend
    key authority. Every load obtains both documents again; failures never return
    a previously fetched snapshot.
    """

    def __init__(
        self,
        *,
        lifetime_seconds: int = 300,
        timeout_seconds: float = 15,
        tls_context: ssl.SSLContext | None = None,
        clock=time.time,
    ):
        if (
            type(lifetime_seconds) is not int
            or not 1 <= lifetime_seconds <= 3600
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
            or not callable(clock)
        ):
            raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        self._context = (
            tls_context if tls_context is not None else ssl.create_default_context()
        )
        self._tls()
        self._lifetime = lifetime_seconds
        self._timeout = timeout_seconds
        self._clock = clock

    def _tls(self):
        if (
            not isinstance(self._context, ssl.SSLContext)
            or not self._context.check_hostname
            or self._context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise EvidenceOidcError("evidence.oidc.tls-invalid")

    def _now(self):
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value < 2**63 - 3601
        ):
            raise EvidenceOidcError("evidence.oidc.clock-invalid")
        return value

    def _read(self, path):
        self._tls()
        connection = http.client.HTTPSConnection(
            _HOST, timeout=self._timeout, context=self._context
        )
        response = None
        try:
            connection.request(
                "GET",
                path,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise EvidenceOidcError("evidence.oidc.response-refused")
            content_types = response.headers.get_all("Content-Type") or []
            if len(content_types) != 1 or content_types[0].lower() not in (
                "application/json",
                "application/json; charset=utf-8",
            ):
                raise EvidenceOidcError("evidence.oidc.media-invalid")
            if response.headers.get_all("Content-Encoding"):
                raise EvidenceOidcError("evidence.oidc.encoding-refused")
            lengths = response.headers.get_all("Content-Length") or []
            transfer = response.headers.get_all("Transfer-Encoding") or []
            if transfer and (lengths or transfer != ["chunked"]):
                raise EvidenceOidcError("evidence.oidc.framing-invalid")
            expected = None
            if lengths:
                if len(lengths) != 1 or not re.fullmatch(
                    r"[1-9][0-9]{0,5}", lengths[0]
                ):
                    raise EvidenceOidcError("evidence.oidc.size-invalid")
                expected = int(lengths[0])
                if expected > _LIMIT:
                    raise EvidenceOidcError("evidence.oidc.size-invalid")
            data = bytearray()
            while len(data) <= _LIMIT:
                chunk = response.read1(min(8192, _LIMIT + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if (
                not data
                or len(data) > _LIMIT
                or (expected is not None and len(data) != expected)
            ):
                raise EvidenceOidcError("evidence.oidc.size-invalid")
            return bytes(data)
        except EvidenceOidcError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise EvidenceOidcError("evidence.oidc.transport-failed") from None
        finally:
            if response is not None:
                response.close()
            connection.close()

    def load(self) -> GitHubIssuerKeySet:
        started = self._now()
        fetched_at = int(started)
        expires_at = fetched_at + self._lifetime
        discovery = _json(self._read(_DISCOVERY))
        if (
            discovery.get("issuer") != GITHUB_OIDC_ISSUER
            or discovery.get("jwks_uri") != GITHUB_OIDC_ISSUER + _KEYS
            or discovery.get("id_token_signing_alg_values_supported") != ["RS256"]
        ):
            raise EvidenceOidcError("evidence.oidc.discovery-invalid")
        middle = self._now()
        if not started <= middle < expires_at:
            raise EvidenceOidcError("evidence.oidc.fetch-expired")
        snapshot = GitHubIssuerKeySet(self._read(_KEYS), fetched_at, expires_at)
        document = _json(snapshot.document)
        keys = document.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 32:
            raise EvidenceOidcError("evidence.oidc.keys-invalid")
        for key in keys:
            if not isinstance(key, dict):
                raise EvidenceOidcError("evidence.oidc.keys-invalid")
            _issuer_key(snapshot, key.get("kid"))
        finished = self._now()
        if not middle <= finished < expires_at:
            raise EvidenceOidcError("evidence.oidc.fetch-expired")
        return snapshot
