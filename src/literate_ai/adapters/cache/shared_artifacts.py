"""Verified immutable local baseline for shared cache artifact namespaces."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
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
        lock = namespace_root / ".locks" / f"{manifest.key_identity.digest}.lock"
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
                if object_path.exists():
                    if self._read_regular(object_path, len(payload)) != payload:
                        raise SharedArtifactCacheError(
                            "shared_cache.object_collision",
                            "content-addressed object contains different bytes",
                        )
                else:
                    # Cache-file permissions are host custody, not the product mode.
                    # The latter remains in the exact expected manifest and may name
                    # a mode inside an archive that Windows cannot represent locally.
                    self._atomic_write(object_path, payload, 0o600)
                encoded = canonical_json_bytes(manifest.to_dict())
                if manifest_path.exists():
                    if self._read_regular(manifest_path, 1024 * 1024) != encoded:
                        raise SharedArtifactCacheError(
                            "shared_cache.key_collision",
                            "cache key already binds another manifest",
                        )
                else:
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
            observed = SharedCacheArtifactManifest.from_dict(
                json.loads(self._read_regular(manifest_path, 1024 * 1024))
            )
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
        payload = self._read_regular(object_path, observed.size_bytes)
        if (
            len(payload) != observed.size_bytes
            or _bytes_identity(payload) != observed.payload_identity
        ):
            raise SharedArtifactCacheError(
                "shared_cache.payload_corrupt",
                "cached payload bytes differ from the manifest",
            )
        return payload

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
