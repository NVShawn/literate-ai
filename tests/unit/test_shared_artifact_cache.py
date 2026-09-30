"""Untrusted local package/container/test cache admission and custody."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.cache.shared_artifacts import (
    LocalSharedArtifactCache,
    SharedArtifactCacheError,
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


class SharedArtifactCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.configuration = _configuration()
        self.cache = LocalSharedArtifactCache(self.configuration, self.root)
        self.payload = b"package-bytes\x00\xff"
        self.manifest = _manifest(self.configuration, self.payload)

    def test_cold_miss_then_verified_warm_hit(self):
        self.assertIsNone(self.cache.get(self.manifest))
        self.cache.put(self.manifest, self.payload)
        self.assertEqual(self.cache.get(self.manifest), self.payload)

    def test_concurrent_identical_publication_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tuple(
                pool.map(
                    lambda _index: self.cache.put(self.manifest, self.payload),
                    range(16),
                )
            )
        self.assertEqual(self.cache.get(self.manifest), self.payload)

    def test_corrupted_payload_and_poisoned_manifest_are_typed_refusals(self):
        self.cache.put(self.manifest, self.payload)
        namespace = self.root / "release" / "package"
        payload_path = namespace / "objects" / self.manifest.payload_identity.digest
        payload_path.write_bytes(b"substituted")
        with self.assertRaisesRegex(SharedArtifactCacheError, "bytes differ"):
            self.cache.get(self.manifest)

        payload_path.write_bytes(self.payload)
        payload_path.chmod(self.manifest.mode)
        manifest_path = (
            namespace / "manifests" / f"{self.manifest.key_identity.digest}.json"
        )
        manifest_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(SharedArtifactCacheError, "schema"):
            self.cache.get(self.manifest)

    def test_changed_authority_and_configuration_are_never_hits(self):
        self.cache.put(self.manifest, self.payload)
        changed = replace(
            self.manifest,
            authority_identities=(canonical_identity("different-authority"),),
        )
        with self.assertRaisesRegex(SharedArtifactCacheError, "complete authority"):
            self.cache.get(changed)

        other_configuration = _configuration(SharedCacheAccessMode.READ_ONLY)
        other = LocalSharedArtifactCache(other_configuration, self.root)
        self.assertEqual(other.get(self.manifest), self.payload)
        other = LocalSharedArtifactCache(
            replace(other_configuration, scope=SharedCacheScope.ORGANIZATION), self.root
        )
        with self.assertRaisesRegex(SharedArtifactCacheError, "another cache"):
            other.get(self.manifest)

    def test_read_only_and_incomplete_package_authority_fail_closed(self):
        read_only = _configuration(SharedCacheAccessMode.READ_ONLY)
        cache = LocalSharedArtifactCache(read_only, self.root)
        manifest = _manifest(read_only, self.payload)
        with self.assertRaisesRegex(SharedArtifactCacheError, "does not authorize"):
            cache.put(manifest, self.payload)

        with self.assertRaisesRegex(SharedArtifactCacheError, "target, ABI, and SBOM"):
            replace(
                self.manifest,
                target_identity=None,
                abi_identity=None,
                sbom_identity=None,
            )

    def test_payload_bytes_and_size_policy_are_verified_before_publication(self):
        with self.assertRaisesRegex(SharedArtifactCacheError, "exact manifest"):
            self.cache.put(self.manifest, b"wrong")
        small_configuration = replace(self.configuration, maximum_bytes=4)
        cache = LocalSharedArtifactCache(small_configuration, self.root)
        manifest = _manifest(small_configuration, self.payload)
        with self.assertRaisesRegex(SharedArtifactCacheError, "size policy"):
            cache.put(manifest, self.payload)


if __name__ == "__main__":
    unittest.main()
