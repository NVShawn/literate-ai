"""Resolve provider-neutral cache authority into Bazel and sccache settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from literate_ai.contracts.shared_cache import (
    SharedCacheAccessMode,
    SharedCacheConfiguration,
    SharedCacheNamespace,
)


class SharedCacheAdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BazelCachePlan:
    arguments: tuple[str, ...]
    credential_reference: str | None


@dataclass(frozen=True, slots=True)
class SccacheCachePlan:
    environment: tuple[tuple[str, str], ...]
    credential_reference: str | None


def _root(root: Path) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        raise SharedCacheAdapterError(
            "shared_cache.root_not_absolute",
            "shared cache root binding must be absolute",
        )
    if candidate.exists() and (candidate.is_symlink() or not candidate.is_dir()):
        raise SharedCacheAdapterError(
            "shared_cache.root_unsafe",
            "shared cache root binding must be a real directory",
        )
    return candidate


def _remote(configuration: SharedCacheConfiguration, suffix: str) -> str | None:
    if configuration.endpoint is None:
        return None
    parts = (configuration.namespace, suffix)
    return (
        configuration.endpoint.rstrip("/")
        + "/"
        + "/".join(quote(item, safe="") for item in parts)
    )


def bazel_cache_plan(
    configuration: SharedCacheConfiguration, *, local_root: Path
) -> BazelCachePlan:
    if not isinstance(configuration, SharedCacheConfiguration):
        raise TypeError("Bazel cache planning requires typed configuration")
    policy = configuration.policy(SharedCacheNamespace.BAZEL)
    root = _root(local_root) / configuration.namespace / "bazel"
    arguments = [
        f"--disk_cache={root}",
        f"--experimental_disk_cache_gc_max_size={configuration.maximum_bytes}",
        (f"--experimental_disk_cache_gc_max_age={configuration.retention_seconds}s"),
    ]
    remote = _remote(configuration, "bazel")
    if remote is not None:
        arguments.append(f"--remote_cache={remote}")
    if policy.mode is SharedCacheAccessMode.READ_ONLY:
        arguments.append("--remote_upload_local_results=false")
    return BazelCachePlan(tuple(arguments), configuration.credential_reference)


def sccache_cache_plan(
    configuration: SharedCacheConfiguration, *, local_root: Path
) -> SccacheCachePlan:
    if not isinstance(configuration, SharedCacheConfiguration):
        raise TypeError("sccache planning requires typed configuration")
    policy = configuration.policy(SharedCacheNamespace.COMPILER)
    root = _root(local_root) / configuration.namespace / "compiler"
    mode = (
        "READ_WRITE" if policy.mode is SharedCacheAccessMode.READ_WRITE else "READ_ONLY"
    )
    environment = {
        "SCCACHE_CACHE_SIZE": str(configuration.maximum_bytes),
        "SCCACHE_DIR": str(root),
        "SCCACHE_LOCAL_RW_MODE": mode,
    }
    if configuration.endpoint is not None:
        environment.update(
            {
                "SCCACHE_MULTILEVEL_CHAIN": "disk,webdav",
                "SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY": "ignore",
                "SCCACHE_WEBDAV_ENDPOINT": configuration.endpoint,
                "SCCACHE_WEBDAV_KEY_PREFIX": (f"{configuration.namespace}/compiler"),
                "SCCACHE_WEBDAV_RW_MODE": mode,
            }
        )
    return SccacheCachePlan(
        tuple(sorted(environment.items())), configuration.credential_reference
    )


__all__ = [
    "BazelCachePlan",
    "SccacheCachePlan",
    "SharedCacheAdapterError",
    "bazel_cache_plan",
    "sccache_cache_plan",
]
