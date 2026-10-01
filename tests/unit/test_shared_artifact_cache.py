"""Untrusted local package/container/test cache admission and custody."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.cache.shared_artifacts import (
    LocalSharedArtifactCache,
    SharedArtifactCacheError,
    SharedCacheArtifactManifest,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    HashAlgorithm,
    canonical_identity,
    canonical_json_bytes,
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

    def test_writer_evicts_oldest_entry_to_enforce_aggregate_quota(self):
        payload = b"x" * 2048
        manifests = [
            replace(
                _manifest(self.configuration, payload),
                key_identity=canonical_identity(i),
            )
            for i in range(3)
        ]
        size = len(payload) + len(canonical_json_bytes(manifests[0].to_dict()))
        cache = LocalSharedArtifactCache(
            replace(self.configuration, maximum_bytes=size), self.root
        )
        for manifest in manifests:
            cache.put(manifest, payload)
        self.assertIsNone(cache.get(manifests[0]))
        self.assertIsNone(cache.get(manifests[1]))
        self.assertEqual(cache.get(manifests[2]), payload)
        files = [
            p
            for directory in ("objects", "manifests")
            for p in (self.root / "release" / "package" / directory).iterdir()
        ]
        self.assertLessEqual(sum(p.stat().st_size for p in files), size)

    def test_expired_read_only_lookup_is_a_miss_without_mutation(self):
        self.cache.put(self.manifest, self.payload)
        path = (
            self.root
            / "release"
            / "package"
            / "manifests"
            / f"{self.manifest.key_identity.digest}.json"
        )
        os.utime(path, (1, 1))
        before = {
            p: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in self.root.rglob("*")
            if p.is_file()
        }
        reader = LocalSharedArtifactCache(
            _configuration(SharedCacheAccessMode.READ_ONLY), self.root
        )
        self.assertIsNone(reader.get(self.manifest))
        self.assertEqual(
            before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
        )

    def test_expired_manifest_collection_keeps_a_shared_live_payload(self):
        self.cache.put(self.manifest, self.payload)
        live = replace(self.manifest, key_identity=canonical_identity("live"))
        self.cache.put(live, self.payload)
        expired = (
            self.root
            / "release"
            / "package"
            / "manifests"
            / f"{self.manifest.key_identity.digest}.json"
        )
        os.utime(expired, (1, 1))
        other = replace(
            _manifest(self.configuration, b"other"),
            key_identity=canonical_identity("other"),
        )
        self.cache.put(other, b"other")
        self.assertFalse(expired.exists())
        self.assertEqual(self.cache.get(live), self.payload)

    def test_distinct_concurrent_writers_cannot_exceed_quota(self):
        payloads = [bytes([i]) * 2048 for i in range(8)]
        manifests = [
            replace(
                _manifest(self.configuration, p), key_identity=canonical_identity(i)
            )
            for i, p in enumerate(payloads)
        ]
        size = 2 * (2048 + len(canonical_json_bytes(manifests[0].to_dict())))
        cache = LocalSharedArtifactCache(
            replace(self.configuration, maximum_bytes=size), self.root
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            tuple(
                pool.map(
                    lambda pair: cache.put(*pair), zip(manifests, payloads, strict=True)
                )
            )
        files = [
            p
            for directory in ("objects", "manifests")
            for p in (self.root / "release" / "package" / directory).iterdir()
        ]
        self.assertLessEqual(sum(p.stat().st_size for p in files), size)
        hits = [
            (m, p)
            for m, p in zip(manifests, payloads, strict=True)
            if cache.get(m) is not None
        ]
        self.assertEqual(len(hits), 2)
        for manifest, payload in hits:
            self.assertEqual(cache.get(manifest), payload)

    def test_concurrent_identical_publication_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tuple(
                pool.map(
                    lambda _index: self.cache.put(self.manifest, self.payload),
                    range(16),
                )
            )
        self.assertEqual(self.cache.get(self.manifest), self.payload)

    def test_quota_counts_shared_payload_only_once(self):
        other = replace(self.manifest, key_identity=canonical_identity("other"))
        size = len(self.payload) + 2 * len(
            canonical_json_bytes(self.manifest.to_dict())
        )
        cache = LocalSharedArtifactCache(
            replace(self.configuration, maximum_bytes=size), self.root
        )
        cache.put(self.manifest, self.payload)
        cache.put(other, self.payload)
        self.assertEqual(cache.get(self.manifest), self.payload)
        self.assertEqual(cache.get(other), self.payload)

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

    def test_unsafe_inventory_refuses_before_any_eviction(self):
        self.cache.put(self.manifest, self.payload)
        root = self.root / "release" / "package"
        unsafe = root / "objects" / "unexpected"
        unsafe.write_bytes(b"foreign")
        expired = root / "manifests" / f"{self.manifest.key_identity.digest}.json"
        os.utime(expired, (1, 1))
        other = replace(self.manifest, key_identity=canonical_identity("other"))
        with self.assertRaisesRegex(SharedArtifactCacheError, "unsafe entry"):
            self.cache.put(other, self.payload)
        self.assertTrue(expired.exists())
        self.assertEqual(unsafe.read_bytes(), b"foreign")

    def test_collision_is_refused_before_expired_entries_are_collected(self):
        self.cache.put(self.manifest, self.payload)
        path = (
            self.root
            / "release"
            / "package"
            / "manifests"
            / f"{self.manifest.key_identity.digest}.json"
        )
        os.utime(path, (1, 1))
        changed = replace(
            self.manifest, provider_identity=canonical_identity("foreign")
        )
        with self.assertRaisesRegex(SharedArtifactCacheError, "another manifest"):
            self.cache.put(changed, self.payload)
        self.assertEqual(
            path.read_bytes(), canonical_json_bytes(self.manifest.to_dict())
        )

    def test_inventory_budget_refuses_before_mutation(self):
        self.cache.put(self.manifest, self.payload)
        other = replace(self.manifest, key_identity=canonical_identity("other"))
        with patch(
            "literate_ai.adapters.cache.shared_artifacts._MAX_INVENTORY_ENTRIES", 1
        ):
            with self.assertRaisesRegex(SharedArtifactCacheError, "finite budget"):
                self.cache.put(other, self.payload)
        self.assertEqual(self.cache.get(self.manifest), self.payload)
        self.assertIsNone(self.cache.get(other))

    def test_publication_cannot_outgrow_its_next_inventory(self):
        manifest_bytes = len(canonical_json_bytes(self.manifest.to_dict()))
        for limit, bound in (
            ("_MAX_INVENTORY_ENTRIES", 2),
            ("_MAX_INVENTORY_MANIFEST_BYTES", manifest_bytes),
        ):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                cache = LocalSharedArtifactCache(
                    self.configuration, Path(directory).resolve()
                )
                with patch(
                    "literate_ai.adapters.cache.shared_artifacts." + limit, bound
                ):
                    for index in range(4):
                        manifest = replace(
                            self.manifest, key_identity=canonical_identity(index)
                        )
                        cache.put(manifest, self.payload)
                        self.assertEqual(cache.get(manifest), self.payload)
                    manifests = cache.root / "package" / "manifests"
                    self.assertEqual(len(list(manifests.iterdir())), 1)

    def test_writer_refuses_manifest_that_reader_cannot_load(self):
        with patch("literate_ai.adapters.cache.shared_artifacts._MANIFEST_BYTES", 32):
            with self.assertRaisesRegex(SharedArtifactCacheError, "reader byte limit"):
                self.cache.put(self.manifest, self.payload)
        self.assertIsNone(self.cache.get(self.manifest))

    def test_retention_refuses_hardlinked_object_without_modifying_either_name(self):
        self.cache.put(self.manifest, self.payload)
        object_path = (
            self.root
            / "release"
            / "package"
            / "objects"
            / self.manifest.payload_identity.digest
        )
        foreign = self.root / "external"
        os.link(object_path, foreign)
        other = replace(self.manifest, key_identity=canonical_identity("other"))
        with self.assertRaisesRegex(SharedArtifactCacheError, "unsafe entry"):
            self.cache.put(other, self.payload)
        self.assertEqual(foreign.read_bytes(), self.payload)
        self.assertEqual(object_path.read_bytes(), self.payload)

    def test_exact_republication_renews_expired_manifest_without_rewriting_object(self):
        self.cache.put(self.manifest, self.payload)
        namespace = self.root / "release" / "package"
        manifest_path = (
            namespace / "manifests" / f"{self.manifest.key_identity.digest}.json"
        )
        object_path = namespace / "objects" / self.manifest.payload_identity.digest
        os.utime(manifest_path, (1, 1))
        previous = object_path.stat().st_mtime_ns
        self.cache.put(self.manifest, self.payload)
        self.assertEqual(self.cache.get(self.manifest), self.payload)
        self.assertEqual(object_path.stat().st_mtime_ns, previous)

    def test_interrupted_publication_orphan_is_collected_by_next_writer(self):
        write = self.cache._atomic_write

        def interrupted(path, content, mode):
            if path.parent.name == "manifests":
                raise OSError("interrupted publication")
            return write(path, content, mode)

        with patch.object(self.cache, "_atomic_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.cache.put(self.manifest, self.payload)
        self.assertIsNone(self.cache.get(self.manifest))
        other = replace(
            _manifest(self.configuration, b"next"),
            key_identity=canonical_identity("next"),
        )
        self.cache.put(other, b"next")
        self.assertEqual(self.cache.get(other), b"next")
        orphan = (
            self.root
            / "release"
            / "package"
            / "objects"
            / self.manifest.payload_identity.digest
        )
        self.assertFalse(orphan.exists())

    def test_reader_returns_miss_if_writer_evicts_after_manifest_read(self):
        size = len(self.payload) + len(canonical_json_bytes(self.manifest.to_dict()))
        cache = LocalSharedArtifactCache(
            replace(self.configuration, maximum_bytes=size), self.root
        )
        cache.put(self.manifest, self.payload)
        other_payload = b"z" * len(self.payload)
        other = replace(
            _manifest(self.configuration, other_payload),
            key_identity=canonical_identity("next"),
        )
        read = cache._read_regular

        def evict(path, maximum_bytes):
            content = read(path, maximum_bytes)
            if path.parent.name == "manifests":
                with patch.object(cache, "_read_regular", side_effect=read):
                    cache.put(other, other_payload)
            return content

        with patch.object(cache, "_read_regular", side_effect=evict):
            self.assertIsNone(cache.get(self.manifest))
        self.assertEqual(cache.get(other), other_payload)

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
