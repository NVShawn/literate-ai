"""Private cache configuration, exact revalidation, and temporary credentials."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    path_is_link_or_reparse,
    require_safe_directory,
)
from literate_ai.adapters.compiler_cache_dependencies import CompilerCacheDependencies
from literate_ai.adapters.dependencies import DependencyObservationError
from literate_ai.adapters.read_only_cache import copy_read_only_cache
from literate_ai.adapters.shared_cache import bazel_cache_plan
from literate_ai.adapters.user_assets import (
    SHARED_CACHE_CONFIG_ENVIRONMENT,
    resolve_shared_cache_config_path,
)
from literate_ai.adapters.user_paths import resolve_host_paths
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.shared_cache import (
    SharedCacheAccessMode,
    SharedCacheConfiguration,
    SharedCacheNamespace,
)

_MAX_CONFIGURATION_BYTES = 1024 * 1024

if TYPE_CHECKING:
    from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding


class SharedCacheConfigurationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _read(path: Path) -> bytes:
    try:
        require_safe_directory(path.parent)
        before = path.lstat()
        if path_is_link_or_reparse(path) or not stat.S_ISREG(before.st_mode):
            raise OSError("configuration is not a regular file")
        if before.st_size > _MAX_CONFIGURATION_BYTES:
            raise OSError("configuration exceeds size bound")
        with path.open("rb") as stream:
            content = stream.read(_MAX_CONFIGURATION_BYTES + 1)
        after = path.lstat()
        if len(content) > _MAX_CONFIGURATION_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise OSError("configuration changed while reading")
        return content
    except (OSError, ValueError, UnsafeFilesystemPathError) as exc:
        raise SharedCacheConfigurationError(
            "shared_cache.configuration_unavailable",
            "private shared-cache configuration is unavailable or unsafe",
        ) from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class BoundSharedCache:
    configuration: SharedCacheConfiguration
    path: Path
    original: bytes = field(repr=False)
    local_root: Path
    environment: Mapping[str, str] = field(repr=False, compare=False)
    compiler_tool: LocalComponentToolBinding | None = None
    compiler_dependencies: CompilerCacheDependencies | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "environment", MappingProxyType(dict(self.environment))
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/bound-shared-cache@1",
                "configuration_identity": self.configuration.identity.uri,
                "local_root": str(self.local_root),
                **(
                    {"compiler_cache_tool": self.compiler_tool.toolchain_identity.uri}
                    if self.compiler_tool is not None
                    else {}
                ),
                **(
                    {
                        "compiler_cache_dependencies": (
                            self.compiler_dependencies.identity.uri
                        )
                    }
                    if self.compiler_dependencies is not None
                    else {}
                ),
            }
        )

    def require_unchanged(self) -> None:
        if self.compiler_tool is not None:
            self.compiler_tool.require_unchanged()
        if _read(self.path) != self.original:
            raise SharedCacheConfigurationError(
                "shared_cache.configuration_changed",
                "private cache configuration changed",
            )
        try:
            require_safe_directory(self.local_root, allow_missing=True)
            for policy in self.configuration.policies:
                require_safe_directory(
                    self.local_root
                    / self.configuration.namespace
                    / policy.namespace.value,
                    allow_missing=True,
                )
        except UnsafeFilesystemPathError as exc:
            raise SharedCacheConfigurationError(
                "shared_cache.root_unsafe", "private cache root is unsafe"
            ) from exc

    def configured(self, namespace: SharedCacheNamespace) -> bool:
        return any(item.namespace is namespace for item in self.configuration.policies)

    def bind_compiler_tool(self) -> BoundSharedCache:
        """Freeze the selected cache executable before authorizing compiler work."""
        if not self.configured(SharedCacheNamespace.COMPILER):
            return self
        from literate_ai.adapters.builders import run_bounded_process
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalComponentToolBinding,
        )

        selected = self.environment.get("LITAI_SCCACHE") or shutil.which(
            "sccache", path=self.environment.get("PATH", os.defpath)
        )
        if selected is None or not Path(selected).is_absolute():
            raise SharedCacheConfigurationError(
                "shared_cache.compiler_tool_missing",
                "compiler caching requires sccache on PATH or absolute LITAI_SCCACHE",
            )
        try:
            tool = LocalComponentToolBinding(selected)
        except (OSError, ValueError) as exc:
            raise SharedCacheConfigurationError(
                "shared_cache.compiler_tool_unavailable",
                "selected compiler-cache executable is unavailable",
            ) from exc
        observed = run_bounded_process(
            (*tool.command, "--version"),
            cwd=self.path.parent,
            environment=dict(self.environment),
            timeout_seconds=10,
            stdout_limit_bytes=4096,
            stderr_limit_bytes=4096,
            error_prefix="shared_cache.compiler_tool",
        )
        version = re.fullmatch(rb"sccache (\d+)\.(\d+)\.(\d+)\s*", observed.stdout)
        if (
            observed.returncode
            or version is None
            or tuple(map(int, version.groups())) < (0, 18, 0)
        ):
            raise SharedCacheConfigurationError(
                "shared_cache.compiler_tool_unsupported",
                "compiler caching requires sccache 0.18.0 or newer",
            )
        tool.require_unchanged()
        try:
            dependencies = CompilerCacheDependencies.observe(tool, self.environment)
        except (OSError, ValueError, DependencyObservationError) as exc:
            raise SharedCacheConfigurationError(
                "shared_cache.compiler_dependencies_unavailable",
                "cannot establish the compiler-cache native dependency closure",
            ) from exc
        return replace(self, compiler_tool=tool, compiler_dependencies=dependencies)

    def require_compiler_dependencies(self, environment: Mapping[str, str]) -> None:
        if self.compiler_tool is None or self.compiler_dependencies is None:
            raise SharedCacheConfigurationError(
                "shared_cache.compiler_dependencies_missing",
                "compiler caching requires a bound native dependency closure",
            )
        try:
            current = CompilerCacheDependencies.observe(self.compiler_tool, environment)
        except (OSError, ValueError, DependencyObservationError) as exc:
            raise SharedCacheConfigurationError(
                "shared_cache.compiler_dependencies_unavailable",
                "cannot revalidate the compiler-cache native dependency closure",
            ) from exc
        if current != self.compiler_dependencies:
            raise SharedCacheConfigurationError(
                "shared_cache.compiler_dependencies_changed",
                "compiler-cache native dependency closure changed",
            )

    def bazel_arguments(self, *, workspace: Path | None = None) -> tuple[str, ...]:
        self.require_unchanged()
        if not self.configured(SharedCacheNamespace.BAZEL):
            return ()
        view = None
        if (
            workspace is not None
            and self.configuration.policy(SharedCacheNamespace.BAZEL).mode
            is SharedCacheAccessMode.READ_ONLY
        ):
            view = workspace / "cache-view"
            copy_read_only_cache(
                self.local_root / self.configuration.namespace / "bazel",
                view,
                maximum_bytes=self.configuration.maximum_bytes,
                retention_seconds=self.configuration.retention_seconds,
            )
        plan = bazel_cache_plan(
            self.configuration, local_root=self.local_root, read_only_view=view
        )
        return (
            *plan.arguments,
            "--remote_verify_downloads=true",
            "--remote_timeout=5s",
            "--remote_retries=0",
            "--remote_local_fallback=true",
            "--incompatible_remote_local_fallback_for_remote_cache=true",
        )

    def credential(self) -> str | None:
        reference = self.configuration.credential_reference
        if reference is None:
            return None
        if re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", reference) is None:
            raise SharedCacheConfigurationError(
                "shared_cache.credential_provider_unsupported",
                "cache credentials require an explicit env:NAME reference",
            )
        token = self.environment.get(reference[4:], "")
        if (
            not token
            or len(token) > 8192
            or any(not 33 <= ord(char) <= 126 for char in token)
        ):
            raise SharedCacheConfigurationError(
                "shared_cache.credential_unavailable",
                "cache credential is absent or invalid",
            )
        return token

    @contextmanager
    def bazel_credentials(self, object_root: Path) -> Iterator[tuple[str, ...]]:
        self.require_unchanged()
        token = (
            self.credential() if self.configured(SharedCacheNamespace.BAZEL) else None
        )
        if token is None:
            yield ()
            return
        # The credential is never part of an argv, plan, receipt, or project file.
        # The private RC survives only for this one invocation.
        with tempfile.TemporaryDirectory(
            prefix="cache-auth-", dir=object_root
        ) as directory:
            path = Path(directory) / "auth.rc"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(
                    "build "
                    + shlex.quote("--remote_cache_header=Authorization=Bearer " + token)
                    + "\n"
                )
            yield (f"--bazelrc={path}",)


def load_shared_cache(
    *,
    environment: Mapping[str, str] | None = None,
) -> BoundSharedCache | None:
    configured = dict(os.environ if environment is None else environment)
    path = resolve_shared_cache_config_path(environment=configured)
    if not path.exists() and not path.is_symlink():
        if configured.get(SHARED_CACHE_CONFIG_ENVIRONMENT):
            raise SharedCacheConfigurationError(
                "shared_cache.configuration_missing",
                "selected cache configuration is missing",
            )
        return None
    original = _read(path)
    try:
        configuration = SharedCacheConfiguration.from_dict(
            json.loads(original, object_pairs_hook=_unique_object)
        )
    except (ValueError, TypeError) as exc:
        raise SharedCacheConfigurationError(
            "shared_cache.configuration_invalid",
            "invalid private shared-cache configuration",
        ) from exc
    host = resolve_host_paths(environment=configured)
    root = Path(host.cache_root) / "shared" / configuration.local_root_reference
    binding = BoundSharedCache(configuration, path, original, root, configured)
    binding.require_unchanged()
    binding.credential()
    return binding
