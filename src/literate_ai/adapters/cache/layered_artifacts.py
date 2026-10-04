"""Local-first artifact reuse with explicit network availability evidence."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.adapters.cache.http_artifacts import HttpSharedArtifactCache
from literate_ai.adapters.cache.shared_artifacts import (
    LocalSharedArtifactCache,
    SharedArtifactCacheError,
    SharedCacheArtifactManifest,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


@dataclass(frozen=True, slots=True)
class SharedArtifactLookup:
    manifest_identity: ContentIdentity
    payload: bytes | None
    source: str
    unavailable_code: str | None = None

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/shared-artifact-lookup@1",
                "manifest_identity": self.manifest_identity.uri,
                "source": self.source,
                "unavailable_code": self.unavailable_code,
            }
        )


class LayeredSharedArtifactCache:
    """Availability is optional; corrupt or foreign evidence remains a refusal."""

    def __init__(
        self,
        local: LocalSharedArtifactCache,
        remote: HttpSharedArtifactCache | None = None,
    ) -> None:
        if not isinstance(local, LocalSharedArtifactCache):
            raise TypeError("local cache must be typed")
        if remote is not None and (
            not isinstance(remote, HttpSharedArtifactCache)
            or remote.configuration != local.configuration
        ):
            raise ValueError("cache layers must bind the same configuration")
        self.local = local
        self.remote = remote

    def lookup(self, expected: SharedCacheArtifactManifest) -> SharedArtifactLookup:
        payload = self.local.get(expected)
        if payload is not None:
            return SharedArtifactLookup(expected.identity, payload, "local")
        if self.remote is None:
            return SharedArtifactLookup(expected.identity, None, "miss")
        try:
            payload = self.remote.get(expected)
        except SharedArtifactCacheError as exc:
            if exc.code not in {
                "shared_cache.unavailable",
                "shared_cache.transport_failed",
            }:
                raise
            return SharedArtifactLookup(expected.identity, None, "miss", exc.code)
        if payload is None:
            return SharedArtifactLookup(expected.identity, None, "miss")
        # Read-only mode covers both tiers. Do not silently populate a local tier
        # where the operator did not authorize writes.
        if self.local.configuration.policy(expected.namespace).mode.can_write:
            try:
                self.local.put(expected, payload)
            except SharedArtifactCacheError as exc:
                if exc.code != "shared_cache.quota_exhausted":
                    raise
                return SharedArtifactLookup(
                    expected.identity, payload, "remote", exc.code
                )
        return SharedArtifactLookup(expected.identity, payload, "remote")

    def get(self, expected: SharedCacheArtifactManifest) -> bytes | None:
        return self.lookup(expected).payload

    def put(self, manifest: SharedCacheArtifactManifest, payload: bytes) -> bool:
        """Publish where capacity permits; report remote publication success."""
        try:
            self.local.put(manifest, payload)
        except SharedArtifactCacheError as exc:
            if exc.code != "shared_cache.quota_exhausted":
                raise
        if self.remote is None:
            return False
        try:
            self.remote.put(manifest, payload)
        except SharedArtifactCacheError as exc:
            if exc.code not in {
                "shared_cache.unavailable",
                "shared_cache.transport_failed",
            }:
                raise
            return False
        return True
