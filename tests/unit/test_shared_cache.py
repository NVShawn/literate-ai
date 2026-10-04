"""Provider-neutral shared-cache authority and tool adapter projections."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.shared_cache import bazel_cache_plan, sccache_cache_plan
from literate_ai.contracts import ContractValidationError
from literate_ai.contracts.shared_cache import (
    SharedCacheAccessMode,
    SharedCacheConfiguration,
    SharedCacheNamespace,
    SharedCacheNamespacePolicy,
    SharedCacheScope,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


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
    def test_configuration_round_trips_and_validates_public_schema(self):
        configured = _configuration()
        self.assertEqual(
            SharedCacheConfiguration.from_dict(configured.to_dict()), configured
        )
        schemas = SchemaCatalog()
        schemas.validate(configured.SCHEMA, configured.to_dict())
        for policy in configured.policies:
            schemas.validate(policy.SCHEMA, policy.to_dict())

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

    def test_bazel_plan_uses_local_and_namespaced_remote_cache_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = bazel_cache_plan(
                _configuration(), local_root=Path(directory).resolve()
            )
        self.assertIn(
            "--disk_cache=",
            plan.arguments,
        )
        self.assertIn(
            "--remote_cache=https://cache.example.invalid/cache/project-main/bazel",
            plan.arguments,
        )
        self.assertIn("--remote_upload_local_results=false", plan.arguments)
        self.assertEqual(plan.credential_reference, "env:CACHE_TOKEN")
        self.assertTrue(all("CACHE_TOKEN" not in item for item in plan.arguments))

    def test_sccache_plan_uses_local_first_webdav_without_secret_material(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = sccache_cache_plan(
                _configuration(), local_root=Path(directory).resolve()
            )
        environment = dict(plan.environment)
        self.assertEqual(environment["SCCACHE_MULTILEVEL_CHAIN"], "disk,webdav")
        self.assertEqual(
            environment["SCCACHE_WEBDAV_ENDPOINT"],
            "https://cache.example.invalid/cache",
        )
        self.assertEqual(
            environment["SCCACHE_WEBDAV_KEY_PREFIX"], "project-main/compiler"
        )
        self.assertEqual(environment["SCCACHE_WEBDAV_RW_MODE"], "READ_WRITE")
        self.assertEqual(plan.credential_reference, "env:CACHE_TOKEN")
        self.assertNotIn("SCCACHE_WEBDAV_TOKEN", environment)

    def test_local_only_configuration_remains_supported(self):
        configured = _configuration(endpoint=None, credential_reference=None)
        with tempfile.TemporaryDirectory() as directory:
            bazel = bazel_cache_plan(configured, local_root=Path(directory).resolve())
            sccache = sccache_cache_plan(
                configured, local_root=Path(directory).resolve()
            )
        self.assertFalse(
            any(item.startswith("--remote_cache=") for item in bazel.arguments)
        )
        self.assertNotIn("SCCACHE_WEBDAV_ENDPOINT", dict(sccache.environment))


if __name__ == "__main__":
    unittest.main()
