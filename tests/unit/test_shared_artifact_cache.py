"""Untrusted local package/container/test cache admission and custody."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
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

    def test_separate_writer_processes_share_one_quota(self):
        script = """
import sys
from dataclasses import replace
from pathlib import Path
from tests.unit.test_shared_artifact_cache import _configuration, _manifest
from literate_ai.adapters.cache.shared_artifacts import LocalSharedArtifactCache
from literate_ai.contracts.identity import canonical_identity
config = replace(_configuration(), maximum_bytes=6000)
payload = bytes([int(sys.argv[2])]) * 2048
manifest = replace(
    _manifest(config, payload), key_identity=canonical_identity(sys.argv[2])
)
LocalSharedArtifactCache(config, Path(sys.argv[1])).put(manifest, payload)
"""
        processes = []
        try:
            for index in range(4):
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-c", script, str(self.root), str(index)],
                        cwd=Path(__file__).resolve().parents[2],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        namespace = self.root / "release" / "package"
        files = [
            p
            for directory in ("objects", "manifests")
            for p in (namespace / directory).iterdir()
        ]
        self.assertLessEqual(sum(p.stat().st_size for p in files), 6000)
        self.assertEqual(len(list((namespace / "manifests").iterdir())), 1)

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


if __name__ == "__main__":
    unittest.main()
