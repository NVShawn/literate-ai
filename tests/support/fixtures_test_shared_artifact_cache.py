"""Shared test fixtures extracted from test_shared_artifact_cache."""

from __future__ import annotations

import hashlib

from literate_ai.adapters.cache.shared_artifacts import (
    SharedCacheArtifactManifest,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    HashAlgorithm,
    canonical_identity,
)
from literate_ai.contracts.shared_cache import (
    SharedCacheAccessMode,
    SharedCacheConfiguration,
    SharedCacheNamespace,
    SharedCacheNamespacePolicy,
    SharedCacheScope,
)


def _raw_identity(payload: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(payload).hexdigest())


def _configuration(
    mode: SharedCacheAccessMode = SharedCacheAccessMode.READ_WRITE,
) -> SharedCacheConfiguration:
    return SharedCacheConfiguration(
        SharedCacheScope.TEAM,
        "release",
        "cache-root",
        (
            SharedCacheNamespacePolicy(SharedCacheNamespace.PACKAGE, mode),
            SharedCacheNamespacePolicy(SharedCacheNamespace.TEST, mode),
        ),
        1024 * 1024,
        86400,
    )


def _manifest(
    configuration: SharedCacheConfiguration,
    payload: bytes,
    *,
    namespace: SharedCacheNamespace = SharedCacheNamespace.PACKAGE,
) -> SharedCacheArtifactManifest:
    product_fields = (
        {
            "target_identity": canonical_identity("target"),
            "abi_identity": canonical_identity("abi"),
            "sbom_identity": canonical_identity("sbom"),
        }
        if namespace is SharedCacheNamespace.PACKAGE
        else {}
    )
    return SharedCacheArtifactManifest(
        namespace,
        configuration.storage_identity,
        canonical_identity({"action": "package", "input": "accepted-closure"}),
        _raw_identity(payload),
        len(payload),
        0o640,
        "application/vnd.debian.binary-package",
        tuple(
            sorted(
                (canonical_identity("accepted"), canonical_identity("closure")),
                key=lambda item: item.uri,
            )
        ),
        canonical_identity("dpkg-deb-provider"),
        **product_fields,
    )
