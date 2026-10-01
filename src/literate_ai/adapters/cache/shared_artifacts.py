"""Verified immutable local baseline for shared cache artifact namespaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    path_is_link_or_reparse,
    require_safe_directory,
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
)

_MANIFEST_SCHEMA = "literate-ai/shared-cache-artifact-manifest@1"
_MANIFEST_BYTES = 1024 * 1024
_MAX_INVENTORY_ENTRIES = 100_000
_MAX_INVENTORY_MANIFEST_BYTES = 16 * 1024 * 1024
_INVENTORY_SECONDS = 30.0


class SharedArtifactCacheError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _bytes_identity(value: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(value).hexdigest())


@dataclass(frozen=True, slots=True)
class SharedCacheArtifactManifest:
    namespace: SharedCacheNamespace
    cache_configuration_identity: ContentIdentity
    key_identity: ContentIdentity
    payload_identity: ContentIdentity
    size_bytes: int
    mode: int
    media_type: str
    authority_identities: tuple[ContentIdentity, ...]
    provider_identity: ContentIdentity
    target_identity: ContentIdentity | None = None
    abi_identity: ContentIdentity | None = None
    sbom_identity: ContentIdentity | None = None

    def __post_init__(self) -> None:
        identities = (
            self.cache_configuration_identity,
            self.key_identity,
            self.payload_identity,
            self.provider_identity,
        )
        if (
            not isinstance(self.namespace, SharedCacheNamespace)
            or any(not isinstance(item, ContentIdentity) for item in identities)
            or isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
            or isinstance(self.mode, bool)
            or not isinstance(self.mode, int)
            or not 0 <= self.mode <= 0o777
            or not isinstance(self.media_type, str)
            or not self.media_type
            or not self.authority_identities
            or any(
                not isinstance(item, ContentIdentity)
                for item in self.authority_identities
            )
            or tuple(item.uri for item in self.authority_identities)
            != tuple(sorted({item.uri for item in self.authority_identities}))
            or any(
                item is not None and not isinstance(item, ContentIdentity)
                for item in (
                    self.target_identity,
                    self.abi_identity,
                    self.sbom_identity,
                )
            )
        ):
            raise SharedArtifactCacheError(
                "shared_cache.manifest_invalid", "artifact manifest is not typed"
            )
        if self.namespace in {
            SharedCacheNamespace.PACKAGE,
            SharedCacheNamespace.CONTAINER,
        } and any(
            item is None
            for item in (
                self.target_identity,
                self.abi_identity,
                self.sbom_identity,
            )
        ):
            raise SharedArtifactCacheError(
                "shared_cache.product_authority_incomplete",
                "package and container manifests require target, ABI, and SBOM",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _MANIFEST_SCHEMA,
            "namespace": self.namespace.value,
            "cache_configuration_identity": self.cache_configuration_identity.uri,
            "key_identity": self.key_identity.uri,
            "payload_identity": self.payload_identity.uri,
            "size_bytes": self.size_bytes,
            "mode": self.mode,
            "media_type": self.media_type,
            "authority_identities": [item.uri for item in self.authority_identities],
            "provider_identity": self.provider_identity.uri,
            "target_identity": (
                None if self.target_identity is None else self.target_identity.uri
            ),
            "abi_identity": (
                None if self.abi_identity is None else self.abi_identity.uri
            ),
            "sbom_identity": (
                None if self.sbom_identity is None else self.sbom_identity.uri
            ),
        }

    @classmethod
    def from_dict(cls, value: object) -> SharedCacheArtifactManifest:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "namespace",
                "cache_configuration_identity",
                "key_identity",
                "payload_identity",
                "size_bytes",
                "mode",
                "media_type",
                "authority_identities",
                "provider_identity",
                "target_identity",
                "abi_identity",
                "sbom_identity",
            }
            or value.get("schema") != _MANIFEST_SCHEMA
        ):
            raise SharedArtifactCacheError(
                "shared_cache.manifest_invalid", "artifact manifest schema is invalid"
            )
        try:
            authorities = value["authority_identities"]
            if not isinstance(authorities, list):
                raise TypeError
            return cls(
                SharedCacheNamespace(value["namespace"]),
                ContentIdentity.parse_uri(value["cache_configuration_identity"]),
                ContentIdentity.parse_uri(value["key_identity"]),
                ContentIdentity.parse_uri(value["payload_identity"]),
                value["size_bytes"],
                value["mode"],
                value["media_type"],
                tuple(ContentIdentity.parse_uri(item) for item in authorities),
                ContentIdentity.parse_uri(value["provider_identity"]),
                None
                if value["target_identity"] is None
                else ContentIdentity.parse_uri(value["target_identity"]),
                None
                if value["abi_identity"] is None
                else ContentIdentity.parse_uri(value["abi_identity"]),
                None
                if value["sbom_identity"] is None
                else ContentIdentity.parse_uri(value["sbom_identity"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SharedArtifactCacheError(
                "shared_cache.manifest_invalid", "artifact manifest values are invalid"
            ) from exc


class LocalSharedArtifactCache:
    """Store opaque immutable bytes; every hit must match an expected manifest."""

    def __init__(self, configuration: SharedCacheConfiguration, root: Path) -> None:
        if not isinstance(configuration, SharedCacheConfiguration):
            raise TypeError("shared artifact cache requires typed configuration")
        candidate = Path(root)
        if not candidate.is_absolute():
            raise SharedArtifactCacheError(
                "shared_cache.root_not_absolute", "local binding must be absolute"
            )
        if candidate.exists() and (
            path_is_link_or_reparse(candidate) or not candidate.is_dir()
        ):
            raise SharedArtifactCacheError(
                "shared_cache.root_unsafe", "local binding must be a real directory"
            )
        self.configuration = configuration
        self.root = candidate / configuration.namespace

    def put(self, manifest: SharedCacheArtifactManifest, payload: bytes) -> None:
        self._require_manifest(manifest)
        policy = self.configuration.policy(manifest.namespace)
        if policy.mode is not SharedCacheAccessMode.READ_WRITE:
            raise SharedArtifactCacheError(
                "shared_cache.read_only", "namespace does not authorize publication"
            )
        if not isinstance(payload, bytes):
            raise TypeError("shared cache payload must be bytes")
        if len(payload) > self.configuration.maximum_bytes:
            raise SharedArtifactCacheError(
                "shared_cache.payload_oversized",
                "payload exceeds the configured local cache size policy",
            )
        if (
            len(payload) != manifest.size_bytes
            or _bytes_identity(payload) != manifest.payload_identity
        ):
            raise SharedArtifactCacheError(
                "shared_cache.payload_mismatch",
                "payload bytes do not match their exact manifest",
            )
        namespace_root = self.root / manifest.namespace.value
        try:
            ensure_safe_directory(namespace_root / "objects")
            ensure_safe_directory(namespace_root / "manifests")
            ensure_safe_directory(namespace_root / ".locks")
        except UnsafeFilesystemPathError as exc:
            raise SharedArtifactCacheError(
                "shared_cache.root_unsafe", "cache namespace path is unsafe"
            ) from exc
        encoded = canonical_json_bytes(manifest.to_dict())
        if len(encoded) > _MANIFEST_BYTES:
            raise SharedArtifactCacheError(
                "shared_cache.manifest_oversized",
                "manifest exceeds the cache reader byte limit",
            )
        if len(encoded) + len(payload) > self.configuration.maximum_bytes:
            raise SharedArtifactCacheError(
                "shared_cache.quota_exhausted",
                "payload and manifest exceed the aggregate cache size policy",
            )
        lock = namespace_root / ".locks" / "publication.lock"
        try:
            with exclusive_cache_lock(lock):
                object_path = (
                    namespace_root / "objects" / manifest.payload_identity.digest
                )
                manifest_path = (
                    namespace_root
                    / "manifests"
                    / f"{manifest.key_identity.digest}.json"
                )
                # Detect collisions before retention can remove any existing entry.
                if (
                    object_path.exists()
                    and self._read_regular(object_path, len(payload)) != payload
                ):
                    raise SharedArtifactCacheError(
                        "shared_cache.object_collision",
                        "content-addressed object contains different bytes",
                    )
                if (
                    manifest_path.exists()
                    and self._read_regular(manifest_path, _MANIFEST_BYTES) != encoded
                ):
                    raise SharedArtifactCacheError(
                        "shared_cache.key_collision",
                        "cache key already binds another manifest",
                    )
                self._reserve(namespace_root, manifest, encoded)
                if not object_path.exists():
                    # Physical cache permissions are separate from product modes.
                    self._atomic_write(object_path, payload, 0o600)
                if not manifest_path.exists():
                    self._atomic_write(manifest_path, encoded, 0o600)
        except CacheLockError as exc:
            raise SharedArtifactCacheError(
                "shared_cache.lock_unavailable", "cache publication lock failed"
            ) from exc

    def get(self, expected: SharedCacheArtifactManifest) -> bytes | None:
        self._require_manifest(expected)
        namespace_root = self.root / expected.namespace.value
        manifest_path = (
            namespace_root / "manifests" / f"{expected.key_identity.digest}.json"
        )
        if not manifest_path.exists():
            return None
        try:
            if (
                manifest_path.lstat().st_mtime
                < time.time() - self.configuration.retention_seconds
            ):
                return None
            observed = SharedCacheArtifactManifest.from_dict(
                json.loads(self._read_regular(manifest_path, _MANIFEST_BYTES))
            )
        except FileNotFoundError:
            return None
        except SharedArtifactCacheError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return None  # Concurrent writer eviction is an ordinary miss.
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SharedArtifactCacheError(
                "shared_cache.manifest_corrupt", "cached manifest is malformed"
            ) from exc
        if observed != expected:
            raise SharedArtifactCacheError(
                "shared_cache.manifest_mismatch",
                "cached manifest does not match current complete authority",
            )
        object_path = namespace_root / "objects" / observed.payload_identity.digest
        try:
            payload = self._read_regular(object_path, observed.size_bytes)
        except SharedArtifactCacheError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return None  # Partial publication or eviction cannot become a hit.
            raise
        if (
            len(payload) != observed.size_bytes
            or _bytes_identity(payload) != observed.payload_identity
        ):
            raise SharedArtifactCacheError(
                "shared_cache.payload_corrupt",
                "cached payload bytes differ from the manifest",
            )
        return payload

    def _reserve(
        self, root: Path, incoming: SharedCacheArtifactManifest, encoded: bytes
    ) -> None:
        """Inventory before mutation, then evict manifests before unreferenced objects.

        The caller holds the namespace writer lock. Readers do not acquire a writable
        lock and tolerate entries disappearing between their two verified reads.
        """
        deadline = time.monotonic() + _INVENTORY_SECONDS
        count = metadata_bytes = 0
        objects: dict[str, os.stat_result] = {}
        manifests: dict[str, tuple[os.stat_result, SharedCacheArtifactManifest]] = {}

        def check_budget() -> None:
            if count > _MAX_INVENTORY_ENTRIES or time.monotonic() > deadline:
                raise SharedArtifactCacheError(
                    "shared_cache.inventory_exhausted",
                    "cache inventory exceeds its finite budget",
                )

        try:
            for directory, suffix in (("objects", ""), ("manifests", ".json")):
                parent = root / directory
                require_safe_directory(parent)
                with os.scandir(parent) as entries:
                    for entry in entries:
                        count += 1
                        check_budget()
                        path = parent / entry.name
                        metadata = path.lstat()
                        if (
                            re.fullmatch(
                                r"[0-9a-f]{64}" + re.escape(suffix), entry.name
                            )
                            is None
                            or path_is_link_or_reparse(path)
                            or not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_nlink != 1
                        ):
                            raise SharedArtifactCacheError(
                                "shared_cache.inventory_unsafe",
                                "cache inventory contains an unsafe entry",
                            )
                        if directory == "objects":
                            objects[entry.name] = metadata
                            continue
                        metadata_bytes += metadata.st_size
                        if metadata_bytes > _MAX_INVENTORY_MANIFEST_BYTES:
                            raise SharedArtifactCacheError(
                                "shared_cache.inventory_exhausted",
                                "cache manifest inventory exceeds its byte budget",
                            )
                        content = self._read_regular(path, _MANIFEST_BYTES)
                        observed = SharedCacheArtifactManifest.from_dict(
                            json.loads(content)
                        )
                        if (
                            observed.namespace is not incoming.namespace
                            or observed.key_identity.digest + ".json" != entry.name
                            or observed.cache_configuration_identity
                            not in {
                                self.configuration.identity,
                                self.configuration.storage_identity,
                            }
                            or canonical_json_bytes(observed.to_dict()) != content
                        ):
                            raise SharedArtifactCacheError(
                                "shared_cache.inventory_unsafe",
                                "cache manifest inventory has inconsistent custody",
                            )
                        manifests[entry.name] = (metadata, observed)
            check_budget()
            cutoff = time.time() - self.configuration.retention_seconds
            retained = {
                name: value
                for name, value in manifests.items()
                if value[0].st_mtime >= cutoff
                and value[1].payload_identity.digest in objects
            }
            protected = incoming.key_identity.digest + ".json"

            references = Counter(
                value[1].payload_identity.digest for value in retained.values()
            )
            references[incoming.payload_identity.digest] += 1
            retained_metadata_bytes = len(encoded) + sum(
                metadata.st_size
                for name, (metadata, _) in retained.items()
                if name != protected
            )
            total = retained_metadata_bytes + sum(
                incoming.size_bytes
                if digest == incoming.payload_identity.digest
                else objects[digest].st_size
                for digest in references
            )

            def fits() -> bool:
                entry_count = (
                    len(retained) + (protected not in retained) + len(references)
                )
                return (
                    total <= self.configuration.maximum_bytes
                    and retained_metadata_bytes <= _MAX_INVENTORY_MANIFEST_BYTES
                    and entry_count <= _MAX_INVENTORY_ENTRIES
                )

            # Oldest publication first; immutable digest breaks timestamp ties.
            for name in sorted(
                retained, key=lambda name: (retained[name][0].st_mtime_ns, name)
            ):
                check_budget()
                if fits():
                    break
                if name == protected:
                    continue
                metadata, removed = retained.pop(name)
                total -= metadata.st_size
                retained_metadata_bytes -= metadata.st_size
                digest = removed.payload_identity.digest
                references[digest] -= 1
                if not references[digest]:
                    total -= objects[digest].st_size
                    del references[digest]
            referenced = set(references)
            if not fits():
                raise SharedArtifactCacheError(
                    "shared_cache.quota_exhausted",
                    "cache quota cannot admit this entry",
                )
            victims = [
                (root / "manifests" / name, metadata)
                for name, (metadata, _) in manifests.items()
                if name not in retained
            ] + [
                (root / "objects" / digest, metadata)
                for digest, metadata in objects.items()
                if digest not in referenced
            ]
            # Recheck every victim before deleting any, and again at each unlink.
            for path, metadata in victims:
                check_budget()
                self._require_unchanged(path, metadata)
            for path, metadata in victims:
                check_budget()
                self._require_unchanged(path, metadata)
                path.unlink()
        except (
            OSError,
            UnsafeFilesystemPathError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise SharedArtifactCacheError(
                "shared_cache.inventory_unsafe",
                "cache retention inventory is unavailable or unsafe",
            ) from exc

    @staticmethod
    def _require_unchanged(path: Path, expected: os.stat_result) -> None:
        require_safe_directory(path.parent)
        actual = path.lstat()
        if path_is_link_or_reparse(path) or (
            actual.st_dev,
            actual.st_ino,
            actual.st_mode,
            actual.st_size,
            actual.st_mtime_ns,
            actual.st_nlink,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
            expected.st_size,
            expected.st_mtime_ns,
            expected.st_nlink,
        ):
            raise SharedArtifactCacheError(
                "shared_cache.entry_changed", "cache entry changed before retention"
            )

    def _require_manifest(self, manifest: SharedCacheArtifactManifest) -> None:
        if not isinstance(manifest, SharedCacheArtifactManifest):
            raise TypeError("shared cache manifest must be typed")
        if manifest.cache_configuration_identity not in {
            self.configuration.storage_identity,
            self.configuration.identity,  # Retain exact legacy local entries.
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

    @staticmethod
    def _read_regular(path: Path, maximum_bytes: int) -> bytes:
        try:
            require_safe_directory(path.parent)
            before = path.lstat()
            if path_is_link_or_reparse(path) or not stat.S_ISREG(before.st_mode):
                raise OSError
            if before.st_size > maximum_bytes:
                raise SharedArtifactCacheError(
                    "shared_cache.entry_oversized", "cache entry exceeds bound"
                )
            with path.open("rb") as stream:
                content = stream.read(maximum_bytes + 1)
            if len(content) > maximum_bytes:
                raise SharedArtifactCacheError(
                    "shared_cache.entry_oversized", "cache entry exceeds bound"
                )
            after = path.lstat()
        except (OSError, UnsafeFilesystemPathError) as exc:
            raise SharedArtifactCacheError(
                "shared_cache.path_unsafe", "cache entry is unavailable or unsafe"
            ) from exc
        if not stat.S_ISREG(after.st_mode) or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise SharedArtifactCacheError(
                "shared_cache.entry_changed", "cache entry changed during inspection"
            )
        return content

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: int) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".publish-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "LocalSharedArtifactCache",
    "SharedArtifactCacheError",
    "SharedCacheArtifactManifest",
]
