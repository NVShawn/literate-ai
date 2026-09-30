"""Bounded immutable HTTP custody for untrusted shared artifact bytes."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from literate_ai.adapters.cache.shared_artifacts import (
    SharedArtifactCacheError,
    SharedCacheArtifactManifest,
    _bytes_identity,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.shared_cache import SharedCacheConfiguration

_MANIFEST_LIMIT = 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never move private credentials or cache custody.
        return None


class HttpSharedArtifactCache:
    """GET/conditional-PUT transport; a hit is never acceptance evidence.

    The operator provisions the namespace directories and a server honoring
    If-None-Match. Objects are published before manifests. Concurrent identical
    writers are idempotent; an existing different entry is a typed refusal.
    """

    def __init__(
        self,
        configuration: SharedCacheConfiguration,
        *,
        timeout_seconds: float = 30,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(configuration, SharedCacheConfiguration):
            raise TypeError("shared artifact cache requires typed configuration")
        if configuration.endpoint is None:
            raise SharedArtifactCacheError(
                "shared_cache.endpoint_missing", "HTTP cache requires an endpoint"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 300
        ):
            raise ValueError("cache timeout must be finite and within 300 seconds")
        self.configuration = configuration
        self.timeout = timeout_seconds
        self.opener = build_opener(_NoRedirect())
        self._authorization = None
        if configuration.credential_reference is not None:
            if credential_resolver is None:
                raise SharedArtifactCacheError(
                    "shared_cache.credential_unresolved",
                    "cache credential reference has no resolver",
                )
            token = credential_resolver(configuration.credential_reference)
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 8192
                or any(ord(char) < 33 or ord(char) > 126 for char in token)
            ):
                raise SharedArtifactCacheError(
                    "shared_cache.credential_invalid", "invalid cache credential"
                )
            self._authorization = "Bearer " + token

    def _validate(self, manifest: SharedCacheArtifactManifest) -> None:
        if not isinstance(manifest, SharedCacheArtifactManifest):
            raise TypeError("shared cache manifest must be typed")
        if manifest.cache_configuration_identity not in {
            self.configuration.storage_identity,
            self.configuration.identity,
        }:
            raise SharedArtifactCacheError(
                "shared_cache.configuration_mismatch",
                "artifact manifest binds another cache configuration",
            )
        self.configuration.policy(manifest.namespace)
        if manifest.size_bytes > self.configuration.maximum_bytes:
            raise SharedArtifactCacheError(
                "shared_cache.payload_oversized", "artifact exceeds cache size policy"
            )

    def _url(self, manifest: SharedCacheArtifactManifest, leaf: str) -> str:
        assert self.configuration.endpoint is not None
        return "/".join(
            (
                self.configuration.endpoint.rstrip("/"),
                self.configuration.namespace,
                manifest.namespace.value,
                leaf,
            )
        )

    def _request(
        self, url: str, *, limit: int, payload: bytes | None = None
    ) -> bytes | None:
        headers = {"Accept-Encoding": "identity"}
        if self._authorization is not None:
            headers["Authorization"] = self._authorization
        if payload is not None:
            headers["If-None-Match"] = "*"
            headers["Content-Type"] = "application/octet-stream"
        request = Request(
            url,
            data=payload,
            headers=headers,
            method="GET" if payload is None else "PUT",
        )
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status not in (
                    {200} if payload is None else {200, 201, 204}
                ):
                    raise SharedArtifactCacheError(
                        "shared_cache.response_invalid", "unexpected cache response"
                    )
                content = bytearray()
                while True:
                    if time.monotonic() - started >= self.timeout:
                        raise SharedArtifactCacheError(
                            "shared_cache.unavailable",
                            "cache response deadline expired",
                        )
                    chunk = response.read1(min(65536, limit + 1 - len(content)))
                    if not chunk:
                        return bytes(content)
                    content.extend(chunk)
                    if len(content) > limit:
                        raise SharedArtifactCacheError(
                            "shared_cache.response_oversized",
                            "cache response exceeds bound",
                        )
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if payload is None and status == 404:
                return None
            if payload is not None and status == 412:
                return None
            raise SharedArtifactCacheError(
                "shared_cache.transport_failed", f"cache HTTP request failed ({status})"
            ) from None
        except (OSError, URLError, HTTPException):
            raise SharedArtifactCacheError(
                "shared_cache.unavailable", "cache transport unavailable"
            ) from None

    def get(self, expected: SharedCacheArtifactManifest) -> bytes | None:
        self._validate(expected)
        encoded = self._request(
            self._url(expected, f"manifests/{expected.key_identity.digest}.json"),
            limit=_MANIFEST_LIMIT,
        )
        if encoded is None:
            return None
        try:
            observed = SharedCacheArtifactManifest.from_dict(json.loads(encoded))
        except (ValueError, TypeError, SharedArtifactCacheError):
            raise SharedArtifactCacheError(
                "shared_cache.manifest_corrupt", "cached manifest is malformed"
            ) from None
        if observed != expected:
            raise SharedArtifactCacheError(
                "shared_cache.manifest_mismatch",
                "cached manifest differs from authority",
            )
        payload = self._request(
            self._url(expected, f"objects/{expected.payload_identity.digest}"),
            limit=expected.size_bytes,
        )
        if payload is None:
            return None
        if (
            len(payload) != expected.size_bytes
            or _bytes_identity(payload) != expected.payload_identity
        ):
            raise SharedArtifactCacheError(
                "shared_cache.payload_corrupt", "cached payload differs from manifest"
            )
        return payload

    def put(self, manifest: SharedCacheArtifactManifest, payload: bytes) -> None:
        self._validate(manifest)
        if not self.configuration.policy(manifest.namespace).mode.can_write:
            raise SharedArtifactCacheError(
                "shared_cache.read_only", "namespace does not authorize publication"
            )
        if not isinstance(payload, bytes):
            raise TypeError("shared cache payload must be bytes")
        if (
            len(payload) != manifest.size_bytes
            or _bytes_identity(payload) != manifest.payload_identity
        ):
            raise SharedArtifactCacheError(
                "shared_cache.payload_mismatch", "payload differs from manifest"
            )
        for leaf, content in (
            (f"objects/{manifest.payload_identity.digest}", payload),
            (
                f"manifests/{manifest.key_identity.digest}.json",
                canonical_json_bytes(manifest.to_dict()),
            ),
        ):
            url = self._url(manifest, leaf)
            self._request(url, limit=_MANIFEST_LIMIT, payload=content)
            # Re-read even after successful PUT: custody is verified, not inferred
            # from a server's status code or conditional-write implementation.
            if self._request(url, limit=len(content)) != content:
                raise SharedArtifactCacheError(
                    "shared_cache.publication_mismatch", "published cache entry differs"
                )
