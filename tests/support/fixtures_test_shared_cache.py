from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_shared_cache``."""






from literate_ai.contracts.shared_cache import (
    SharedCacheAccessMode,
    SharedCacheConfiguration,
    SharedCacheNamespace,
    SharedCacheNamespacePolicy,
    SharedCacheScope,
)


def _configuration(
    *,
    endpoint: str | None = "https://cache.example.invalid/cache",
    credential_reference: str | None = "env:CACHE_TOKEN",
    bazel_mode: SharedCacheAccessMode = SharedCacheAccessMode.READ_ONLY,
) -> SharedCacheConfiguration:
    return SharedCacheConfiguration(
        SharedCacheScope.TEAM,
        "project-main",
        "team-cache",
        (
            SharedCacheNamespacePolicy(SharedCacheNamespace.BAZEL, bazel_mode),
            SharedCacheNamespacePolicy(
                SharedCacheNamespace.COMPILER, SharedCacheAccessMode.READ_WRITE
            ),
            SharedCacheNamespacePolicy(
                SharedCacheNamespace.CONTAINER, SharedCacheAccessMode.READ_ONLY
            ),
            SharedCacheNamespacePolicy(
                SharedCacheNamespace.PACKAGE, SharedCacheAccessMode.READ_WRITE
            ),
            SharedCacheNamespacePolicy(
                SharedCacheNamespace.TEST, SharedCacheAccessMode.READ_ONLY
            ),
        ),
        10 * 1024 * 1024 * 1024,
        7 * 24 * 60 * 60,
        endpoint,
        True,
        credential_reference,
    )

