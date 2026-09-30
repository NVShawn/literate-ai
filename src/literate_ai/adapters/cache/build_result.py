"""Host-specific immutable build-result cache rooted at ``OBJ_DIR``."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import platform
import re
import shutil
import stat
import sysconfig
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

from literate_ai._cache_lock import (
    CacheLockError,
    cache_lock_path,
    exclusive_cache_lock,
)
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    path_is_link_or_reparse,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.builders import (
    UNSANDBOXED_HOST_BUILD_PROFILE,
    require_unsandboxed_host_build_authorization,
)
from literate_ai.cache_directories import (
    CacheDirectoryError,
    ensure_cache_directory,
    require_cache_directory_current,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.ports import BuildInputConsumption, require_build_result
from literate_ai.security import BuildAuthorization, BuildRequest

_SCHEMA = "literate-ai/local-build-result-cache-entry@3"


class BuildResultCacheError(RuntimeError):
    """An OBJ_DIR result failed provenance or byte-integrity verification."""


class CachedBuildAdapter:
    """Skip compilation only for an exact, byte-verified host build result."""

    def __init__(
        self,
        delegate,
        *,
        object_root: Path,
        project_root: Path,
        namespace_material: Mapping[str, object],
        target_platform: Mapping[str, str] | None = None,
        builder_implementation_identity: str | None = None,
    ) -> None:
        builder_id = getattr(delegate, "builder_id", None)
        if not isinstance(builder_id, str) or not builder_id:
            raise TypeError("build-result cache requires a concrete builder")
        self.delegate = delegate
        self.builder_id = builder_id
        self.builder_implementation_identity = (
            _builder_implementation_identity(delegate)
            if builder_implementation_identity is None
            else ContentIdentity.parse_uri(builder_implementation_identity)
        )
        self.project_root = Path(project_root).resolve(strict=True)
        target_material_identity = canonical_identity(
            {
                "schema": "literate-ai/build-cache-target-material@2",
                "builder_implementation_identity": (
                    self.builder_implementation_identity.uri
                ),
                "material": dict(namespace_material),
            }
        )
        operating_system, architecture, abi, target_triple = _target_platform(
            target_platform
        )
        self.target_namespace = {
            "schema": "literate-ai/build-cache-target-namespace@1",
            "operating_system": operating_system,
            "architecture": architecture,
            "abi": abi,
            "target_triple": target_triple,
            "target_identity": target_material_identity.uri,
        }
        self.target_namespace_identity = canonical_identity(self.target_namespace)
        # The full target identity already binds OS, architecture, ABI, triple, and
        # builder material. Keep the filesystem key exact but compact enough for deeply
        # nested build evidence on hosts with classic path limits.
        target_relative = Path("t") / self.target_namespace_identity.digest
        self.object_root = ensure_cache_directory(
            object_root,
            kind="object",
            project_root=project_root,
            required_subdirectories=(
                target_relative / "r",
                target_relative / "a",
                target_relative / "s",
            ),
        )
        self.target_root = self.object_root / target_relative
        self.records = self.target_root / "r"
        self.namespace_identity = canonical_identity(
            {
                "schema": "literate-ai/build-cache-namespace@3",
                "implementation": "cached-build-adapter@3",
                "builder_id": self.builder_id,
                "builder_implementation_identity": (
                    self.builder_implementation_identity.uri
                ),
                "target_namespace": self.target_namespace,
                "material": dict(namespace_material),
            }
        )
        self.hits = 0
        self.misses = 0
        self.hit_seconds = 0.0
        self.build_seconds = 0.0

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self._build(request, authorization, consumption=None)

    def build_with_input_consumption(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
        consumption: BuildInputConsumption,
    ) -> Mapping[str, object]:
        if not isinstance(consumption, BuildInputConsumption):
            raise TypeError("cached build input consumption must be typed")
        return self._build(request, authorization, consumption=consumption)

    def report(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/local-build-result-cache-report@3",
            "root": str(self.object_root),
            "namespace_identity": self.namespace_identity.uri,
            "builder_implementation_identity": (
                self.builder_implementation_identity.uri
            ),
            "target_namespace": dict(self.target_namespace),
            "target_root": str(self.target_root),
            "hits": self.hits,
            "misses": self.misses,
            "hit_seconds": round(self.hit_seconds, 6),
            "build_seconds": round(self.build_seconds, 6),
        }

    def _build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
        *,
        consumption: BuildInputConsumption | None,
    ) -> Mapping[str, object]:
        key = self._key(request, consumption=consumption)
        record_path = self.records / f"{key.digest}.json"
        started = time.monotonic()
        if record_path.exists():
            self._require_current_authorization(request, authorization)
            result = self._load(
                record_path, request=request, authorization=authorization
            )
            self.hits += 1
            self.hit_seconds += time.monotonic() - started
            return result

        if consumption is None:
            result = dict(self.delegate.build(request, authorization))
        else:
            method = getattr(self.delegate, "build_with_input_consumption", None)
            if not callable(method):
                raise TypeError("delegate does not consume build-system input evidence")
            result = dict(method(request, authorization, consumption))
        normalized = self._validate_result(
            result,
            request=request,
            authorization_id=_authorization_id(authorization),
        )
        origin_authorization_id = str(normalized["authorization_id"])
        artifact_path = self._contain_artifact(Path(str(normalized["artifact_path"])))
        normalized["artifact_path"] = str(artifact_path)
        normalized["cache_origin_authorization_id"] = origin_authorization_id
        tree_identity = _artifact_tree_identity(artifact_path)
        record = {
            "schema": _SCHEMA,
            "key_identity": key.uri,
            "namespace_identity": self.namespace_identity.uri,
            "artifact_tree_identity": tree_identity,
            "result": normalized,
        }
        published_result = self._publish_record(
            record_path,
            key_digest=key.digest,
            record=record,
            normalized=normalized,
            request=request,
            authorization=authorization,
        )
        self.misses += 1
        self.build_seconds += time.monotonic() - started
        return published_result

    def _publish_record(
        self,
        record_path: Path,
        *,
        key_digest: str,
        record: Mapping[str, object],
        normalized: dict[str, object],
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> dict[str, object]:
        root_lock = cache_lock_path(
            self.project_root, self.object_root, "lifecycle.lock"
        )
        try:
            with exclusive_cache_lock(root_lock):
                require_cache_directory_current(
                    self.object_root,
                    kind="object",
                    project_root=self.project_root,
                )
                try:
                    ensure_safe_directory(record_path.parent)
                except UnsafeFilesystemPathError as exc:
                    raise BuildResultCacheError(
                        "build-result cache publication path is unsafe"
                    ) from exc
                artifact_tree_identity = record.get("artifact_tree_identity")
                if (
                    not isinstance(artifact_tree_identity, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_tree_identity)
                    is None
                ):
                    raise BuildResultCacheError(
                        "build-result cache publication has an invalid artifact "
                        "identity"
                    )
                expected_artifact = (
                    self.target_root
                    / "a"
                    / artifact_tree_identity.removeprefix("sha256:")
                )
                artifact = Path(str(normalized.get("artifact_path")))
                if artifact != expected_artifact:
                    raise BuildResultCacheError(
                        "build-result cache publication artifact is outside its exact "
                        "target namespace"
                    )
                try:
                    observed_tree_identity = _artifact_tree_identity(artifact)
                except (OSError, BuildResultCacheError) as exc:
                    raise BuildResultCacheError(
                        "build-result cache publication artifact is unavailable"
                    ) from exc
                if observed_tree_identity != artifact_tree_identity:
                    raise BuildResultCacheError(
                        "build-result cache publication artifact bytes changed"
                    )
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix="build-result-", dir=record_path.parent
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                try:
                    temporary.write_bytes(canonical_json_bytes(record))
                    entry_lock = cache_lock_path(
                        self.project_root,
                        self.object_root,
                        "build-results",
                        f"{key_digest}.lock",
                    )
                    with exclusive_cache_lock(entry_lock):
                        if record_path.exists() or path_is_link_or_reparse(record_path):
                            self._require_current_authorization(request, authorization)
                            return self._load(
                                record_path,
                                request=request,
                                authorization=authorization,
                            )
                        os.replace(temporary, record_path)
                        return normalized
                finally:
                    temporary.unlink(missing_ok=True)
        except (CacheLockError, CacheDirectoryError) as exc:
            raise BuildResultCacheError(
                "build-result cache publication lock is unavailable"
            ) from exc

    def _key(
        self,
        request: Mapping[str, object],
        *,
        consumption: BuildInputConsumption | None,
    ):
        source_digest = _source_digest(request)
        # The effective revision is authorization/provenance, not a compiler input.
        # Require it on every request (and revalidate the current authorization on a
        # hit), but do not throw away byte-identical native artifacts merely because a
        # specification edit regenerated the same source tree.
        authority = {
            "effective_revision_digest": request.get("effective_revision_digest"),
        }
        build_inputs = {
            "source_bundle_digest": source_digest,
            "builder_id": request.get("builder_id"),
            "toolchain_digest": request.get("toolchain_digest"),
            "sandbox_profile": request.get("sandbox_profile"),
            "requested_privileges": request.get("requested_privileges"),
            "allowed_outputs": request.get("allowed_outputs"),
        }
        required_values = (*authority.values(), *build_inputs.values())
        if any(value is None for value in required_values):
            raise BuildResultCacheError("build cache key lacks exact request authority")
        return canonical_identity(
            {
                "schema": "literate-ai/local-build-result-cache-key@3",
                "namespace_identity": self.namespace_identity.uri,
                "target_namespace_identity": self.target_namespace_identity.uri,
                **build_inputs,
                "build_input_consumption": (
                    None if consumption is None else consumption.to_dict()
                ),
            }
        )

    def _load(
        self,
        path: Path,
        *,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            require_safe_directory(path.parent)
        except UnsafeFilesystemPathError as exc:
            raise BuildResultCacheError(
                "build-result cache record path is unsafe"
            ) from exc
        if path_is_link_or_reparse(path) or not path.is_file():
            raise BuildResultCacheError("build-result cache record is unsafe")
        try:
            record = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BuildResultCacheError("build-result cache record is invalid") from exc
        if (
            not isinstance(record, dict)
            or record.get("schema") != _SCHEMA
            or record.get("key_identity") != f"sha256:{path.stem}"
            or record.get("namespace_identity") != self.namespace_identity.uri
            or not isinstance(record.get("result"), dict)
        ):
            raise BuildResultCacheError("build-result cache record has another schema")
        stored_result = dict(record["result"])
        origin_authorization_id = stored_result.get(
            "cache_origin_authorization_id",
            stored_result.get("authorization_id"),
        )
        if not isinstance(origin_authorization_id, str) or not origin_authorization_id:
            raise BuildResultCacheError(
                "build-result cache omits its originating authorization"
            )
        result = self._validate_result(
            stored_result,
            request=request,
            authorization_id=origin_authorization_id,
        )
        artifact_tree_identity = record.get("artifact_tree_identity")
        if (
            not isinstance(artifact_tree_identity, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_tree_identity) is None
        ):
            raise BuildResultCacheError(
                "build-result cache has an invalid artifact tree identity"
            )
        expected_artifact = (
            self.target_root / "a" / artifact_tree_identity.removeprefix("sha256:")
        )
        artifact = Path(str(result["artifact_path"]))
        if artifact != expected_artifact:
            raise BuildResultCacheError(
                "cached build artifact is outside its exact target namespace"
            )
        try:
            require_safe_directory(expected_artifact.parent)
        except UnsafeFilesystemPathError as exc:
            raise BuildResultCacheError("cached build artifact path is unsafe") from exc
        if _artifact_tree_identity(artifact) != artifact_tree_identity:
            raise BuildResultCacheError("cached build artifact bytes changed")
        current_authorization_id = _authorization_id(authorization)
        result["cache_origin_authorization_id"] = origin_authorization_id
        result["authorization_id"] = current_authorization_id
        return self._validate_result(
            result,
            request=request,
            authorization_id=current_authorization_id,
        )

    def _validate_result(
        self,
        value: Mapping[str, object],
        *,
        request: Mapping[str, object],
        authorization_id: str,
    ) -> dict[str, object]:
        source_digest = _source_digest(request)
        toolchain = request.get("toolchain_digest")
        if not all(isinstance(item, str) for item in (source_digest, authorization_id)):
            raise BuildResultCacheError("build result validation lacks exact authority")
        normalized = require_build_result(
            dict(value),
            source_bundle_digest=source_digest,
            authorization_id=authorization_id,
            toolchain_identity=toolchain if isinstance(toolchain, str) else None,
        )
        artifact_path = normalized.get("artifact_path")
        if not isinstance(artifact_path, str) or not artifact_path:
            raise BuildResultCacheError("build result omits its artifact path")
        return dict(normalized)

    def _require_current_authorization(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> None:
        """Recheck the current grant without rebuilding already verified bytes."""

        hook = getattr(self.delegate, "require_cache_hit_authorized", None)
        if callable(hook):
            hook(request, authorization)
            return
        verifier = getattr(self.delegate, "authorization_verifier", None)
        require_valid = getattr(verifier, "require_build_valid", None)
        clock = getattr(self.delegate, "clock", None)
        if not callable(require_valid) or not callable(clock):
            raise BuildResultCacheError(
                "build cache hit requires a current authorization verifier"
            )
        try:
            typed_request = _build_request(request)
            typed_authorization = _build_authorization(authorization)
        except (KeyError, TypeError, ValueError) as exc:
            raise BuildResultCacheError(
                "build cache hit has malformed authorization authority"
            ) from exc
        require_valid(typed_authorization, typed_request, now=clock())
        if typed_request.sandbox_profile == UNSANDBOXED_HOST_BUILD_PROFILE:
            require_unsandboxed_host_build_authorization(
                typed_request, typed_authorization
            )

    def _contain_artifact(self, supplied: Path) -> Path:
        """Copy a first-build artifact beneath this managed OBJ_DIR when needed."""

        root_lock = cache_lock_path(
            self.project_root, self.object_root, "lifecycle.lock"
        )
        try:
            with exclusive_cache_lock(root_lock):
                require_cache_directory_current(
                    self.object_root,
                    kind="object",
                    project_root=self.project_root,
                )
                return self._contain_artifact_locked(supplied)
        except (CacheLockError, CacheDirectoryError) as exc:
            raise BuildResultCacheError(
                "build artifact publication lock is unavailable"
            ) from exc

    def _contain_artifact_locked(self, supplied: Path) -> Path:

        if path_is_link_or_reparse(supplied):
            raise BuildResultCacheError(
                "build artifact cannot be a link or reparse point"
            )
        artifact = supplied.resolve(strict=True)
        tree_identity = _artifact_tree_identity(artifact)
        destination_root = self.target_root / "a"
        staging_root = self.target_root / "s"
        try:
            ensure_safe_directory(destination_root)
            ensure_safe_directory(staging_root)
        except UnsafeFilesystemPathError as exc:
            raise BuildResultCacheError(
                "build artifact publication path is unsafe"
            ) from exc
        destination = destination_root / tree_identity.removeprefix("sha256:")
        if artifact == destination:
            return artifact
        staging = Path(tempfile.mkdtemp(prefix="artifact-", dir=staging_root))
        payload = staging / "payload"
        try:
            if artifact.is_file():
                shutil.copy2(artifact, payload)
            else:
                shutil.copytree(artifact, payload, copy_function=shutil.copy2)
            if _artifact_tree_identity(payload) != tree_identity:
                raise BuildResultCacheError(
                    "contained build artifact changed while being copied"
                )
            lock_path = cache_lock_path(
                self.project_root,
                self.object_root,
                "artifacts",
                f"{tree_identity.removeprefix('sha256:')}.lock",
            )
            try:
                with exclusive_cache_lock(lock_path):
                    if destination.exists() or path_is_link_or_reparse(destination):
                        if _artifact_tree_identity(destination) != tree_identity:
                            raise BuildResultCacheError(
                                "contained build artifact identity collided"
                            )
                    else:
                        os.replace(payload, destination)
            except CacheLockError as exc:
                raise BuildResultCacheError(
                    "build artifact publication lock is unavailable"
                ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return destination


def _source_digest(request: Mapping[str, object]) -> object:
    source_digest = request.get("source_bundle_digest")
    if isinstance(source_digest, str):
        return source_digest
    artifact = request.get("artifact")
    return (
        artifact.get("source_bundle_digest") if isinstance(artifact, Mapping) else None
    )


def _authorization_id(authorization: Mapping[str, object]) -> str:
    value = authorization.get("authorization_id")
    if not isinstance(value, str) or not value:
        raise BuildResultCacheError("build authorization omits its identity")
    return value


def _builder_implementation_identity(delegate) -> ContentIdentity:
    """Bind cache reuse to the actual adapter modules that produced native bytes."""

    members: list[dict[str, str]] = []
    seen: set[int] = set()
    current = delegate
    for _depth in range(8):
        marker = id(current)
        if marker in seen:
            raise TypeError("build-result cache builder delegate chain is cyclic")
        seen.add(marker)
        implementation_type = type(current)
        source_name = inspect.getsourcefile(implementation_type)
        if source_name is None:
            raise TypeError(
                "build-result cache requires inspectable builder implementation source"
            )
        source = Path(source_name)
        try:
            before = source.lstat()
            if stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
                raise OSError
            if before.st_size > 4 * 1024 * 1024:
                raise OSError
            content = source.read_bytes()
            after = source.lstat()
        except OSError as exc:
            raise TypeError(
                "build-result cache builder implementation source is unavailable"
            ) from exc
        if (
            len(content) != before.st_size
            or stat_is_link_or_reparse(after)
            or not stat.S_ISREG(after.st_mode)
            or _node_signature(before) != _node_signature(after)
        ):
            raise TypeError(
                "build-result cache builder implementation changed while inspected"
            )
        members.append(
            {
                "module": implementation_type.__module__,
                "qualname": implementation_type.__qualname__,
                "content_identity": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
        nested = getattr(current, "delegate", None)
        if nested is None:
            break
        current = nested
    else:
        raise TypeError("build-result cache builder delegate chain is too deep")
    return canonical_identity(
        {
            "schema": "literate-ai/builder-implementation-closure@1",
            "members": members,
        }
    )


def _build_request(value: Mapping[str, object]) -> BuildRequest:
    fields = {
        key: value[key]
        for key in (
            "effective_revision_digest",
            "builder_id",
            "toolchain_digest",
            "sandbox_profile",
            "requested_privileges",
            "allowed_outputs",
        )
    }
    fields["source_bundle_digest"] = _source_digest(value)
    if "schema" in value:
        fields["schema"] = value["schema"]
    return BuildRequest.from_dict(fields)


def _build_authorization(value: Mapping[str, object]) -> BuildAuthorization:
    return BuildAuthorization.from_dict(
        {
            key: value[key]
            for key in (
                "authorization_id",
                "classification_digest",
                "request_digest",
                "effective_revision_digest",
                "actor",
                "reason",
                "profile",
                "privileges",
                "issued_at",
                "expires_at",
                "warning",
                "revoked",
            )
        }
    )


def _target_platform(
    supplied: Mapping[str, str] | None,
) -> tuple[str, str, str, str]:
    if supplied is None:
        operating_system = platform.system().strip().lower()
        operating_system = {"darwin": "macos"}.get(operating_system, operating_system)
        architecture = platform.machine().strip().lower()
        architecture = {
            "amd64": "x86_64",
            "arm64": "aarch64",
        }.get(architecture, architecture)
        abi = sysconfig.get_platform().strip().lower()
        configured_triple = sysconfig.get_config_var("HOST_GNU_TYPE")
        target_triple = (
            configured_triple.strip().lower()
            if isinstance(configured_triple, str) and configured_triple.strip()
            else f"{architecture}-{operating_system}-{abi}"
        )
    else:
        if set(supplied) != {
            "operating_system",
            "architecture",
            "abi",
            "target_triple",
        }:
            raise ValueError(
                "target_platform requires exactly operating_system, architecture, "
                "abi, target_triple"
            )
        operating_system = supplied["operating_system"].strip().lower()
        architecture = supplied["architecture"].strip().lower()
        abi = supplied["abi"].strip().lower()
        target_triple = supplied["target_triple"].strip().lower()
    if not all((operating_system, architecture, abi, target_triple)):
        raise ValueError("target platform values must be non-empty text")
    return operating_system, architecture, abi, target_triple


def _artifact_tree_identity(path: Path) -> str:
    supplied = Path(path)
    if path_is_link_or_reparse(supplied):
        raise BuildResultCacheError("cached artifact cannot be a link or reparse point")
    root = supplied.resolve(strict=True)
    try:
        metadata = root.lstat()
        require_safe_directory(root if stat.S_ISDIR(metadata.st_mode) else root.parent)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise BuildResultCacheError("cached artifact path is unsafe") from exc
    root_metadata = root.lstat()
    if stat_is_link_or_reparse(root_metadata):
        raise BuildResultCacheError("cached artifact cannot be a link or reparse point")
    if stat.S_ISREG(root_metadata.st_mode):
        content = _read_unchanged_regular_file(root, root_metadata)
        material: object = {
            "kind": "file",
            "size": len(content),
            "mode": root_metadata.st_mode & 0o111,
            "identity": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
    elif stat.S_ISDIR(root_metadata.st_mode):
        files: list[dict[str, object]] = []
        for current, metadata in _artifact_files(root):
            content = _read_unchanged_regular_file(current, metadata)
            files.append(
                {
                    "path": current.relative_to(root).as_posix(),
                    "size": len(content),
                    "mode": metadata.st_mode & 0o111,
                    "identity": "sha256:" + hashlib.sha256(content).hexdigest(),
                }
            )
        if not files:
            raise BuildResultCacheError("cached artifact directory is empty")
        material = {"kind": "directory", "files": files}
    else:
        raise BuildResultCacheError(
            "cached artifact is not a regular file or directory"
        )
    return canonical_identity(
        {"schema": "literate-ai/cached-artifact-tree@1", "artifact": material}
    ).uri


def _artifact_files(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    def walk(directory: Path) -> Iterator[tuple[Path, os.stat_result]]:
        before = directory.lstat()
        if stat_is_link_or_reparse(before) or not stat.S_ISDIR(before.st_mode):
            raise BuildResultCacheError(
                "cached artifact contains a link or reparse point"
            )
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise BuildResultCacheError("cached artifact tree is unavailable") from exc
        for current in children:
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise BuildResultCacheError(
                    "cached artifact tree changed while being inspected"
                ) from exc
            if stat_is_link_or_reparse(metadata):
                raise BuildResultCacheError(
                    "cached artifact contains a link or reparse point"
                )
            if stat.S_ISDIR(metadata.st_mode):
                yield from walk(current)
            elif stat.S_ISREG(metadata.st_mode):
                yield current, metadata
            else:
                raise BuildResultCacheError("cached artifact contains a special node")
        after = directory.lstat()
        if stat_is_link_or_reparse(after) or _node_signature(before) != _node_signature(
            after
        ):
            raise BuildResultCacheError(
                "cached artifact tree changed while being inspected"
            )

    return walk(root)


def _read_unchanged_regular_file(path: Path, before: os.stat_result) -> bytes:
    try:
        content = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise BuildResultCacheError(
            "cached artifact changed while being inspected"
        ) from exc
    if (
        stat_is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or _node_signature(before) != _node_signature(after)
    ):
        raise BuildResultCacheError("cached artifact changed while being inspected")
    return content


def _node_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


__all__ = ["BuildResultCacheError", "CachedBuildAdapter"]
