"""Provider-neutral shared-cache authority and tool adapter projections."""

from __future__ import annotations

import unittest

from literate_ai.contracts import ContractValidationError
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


class SharedCacheTests(unittest.TestCase):
    def test_rejects_credentials_in_urls_plaintext_tls_and_ambiguous_policies(self):
        for endpoint, credential, message in (
            ("https://user:secret@cache.example/cache", None, "credential-free"),
            ("http://cache.example/cache", None, "HTTPS"),
            (None, "env:CACHE_TOKEN", "requires a network endpoint"),
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ContractValidationError, message):
                    _configuration(endpoint=endpoint, credential_reference=credential)
        duplicate = _configuration().policies[:1] * 2
        with self.assertRaisesRegex(ContractValidationError, "uniquely sorted"):
            SharedCacheConfiguration(
                SharedCacheScope.USER,
                "namespace",
                "root",
                duplicate,
                1,
                1,
            )


if __name__ == "__main__":
    unittest.main()
