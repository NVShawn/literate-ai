"""Worker-side execution of one exact, already-selected lifecycle request."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    path_is_link_or_reparse,
    require_safe_directory,
)
from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.directory_artifacts import (
    directory_export_bytes,
    require_library_package,
    require_transported_library_package,
)
from literate_ai.adapters.user_paths import prepare_user_directory, resolve_host_paths
from literate_ai.adapters.worker_capabilities import probe_worker_capabilities
from literate_ai.cache_directories import CacheDirectories, bind_cache_directories
from literate_ai.contracts import (
    MAX_REMOTE_CONTROL_SUMMARY_BYTES,
    MAX_REMOTE_EVIDENCE_MANIFEST_BYTES,
    ContentIdentity,
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionSourceMaterialization,
    ExecutionWorker,
    ExecutionWorkerKind,
    HashAlgorithm,
    LifecycleDispatchAction,
    ObservedExecutionEnvironment,
    RemoteEvidenceFile,
    RemoteFailureDiagnostic,
    RemoteLifecycleEvidenceManifest,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.library_products import (
    LibraryArtifactProduct,
    library_worker_manifest,
)
from literate_ai.diagnostics import inherited_verbose_environment, trace_subprocess
from literate_ai.remote_source_guard import (
    SourceGuardError,
    extract_accepted_source_cache_archive,
    extract_source_archive_with_manifest,
    source_tree_identity,
    verify_materialized_source,
)

from .artifact_exports import ArtifactEntrypointCommand, ArtifactExportError

_CAS_SCHEME = "litai-worker-cas"
_EVIDENCE_URI = "staged:remote-evidence.tar.gz"
_MAX_EVIDENCE_FILES = 50_000
_MAX_EVIDENCE_BYTES = 8 * 1024 * 1024 * 1024
_SECRET = re.compile(
    r"(?i)(authorization|cookie|credential|password|secret|token)"
    r"([\"'\s:=]+)([^\s,\"']+)"
)
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\[^\r\n\t\"']+")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^/\s\"']+/)+[^/\s\"']*")


class RemoteExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class RemoteLifecycleCustody:
    """Exact short-root directory custody for one worker lifecycle attempt."""

    attempt_root: Path
    cache_directories: CacheDirectories
    runtime_root: Path
    candidate_receipt: Path

    def __post_init__(self) -> None:
        root = self.attempt_root.resolve(strict=True)
        paths = (
            self.cache_directories.build_dir,
            self.cache_directories.obj_dir,
            self.runtime_root,
            self.candidate_receipt,
        )
        if any(not Path(path).resolve().is_relative_to(root) for path in paths):
            raise ValueError(
                "worker lifecycle custody must remain below its attempt root"
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/remote-lifecycle-directory-custody@1",
                "attempt_root": str(self.attempt_root),
                "cache_directory_custody_identity": (
                    self.cache_directories.identity.uri
                ),
                "runtime_root": str(self.runtime_root),
                "candidate_receipt": str(self.candidate_receipt),
            }
        )


class RemoteLifecycleRebuilder(Protocol):
    """CLI-independent port for one worker-local Standard lifecycle rebuild."""

    def __call__(
        self,
        *,
        component_path: str,
        project_root: Path,
        custody: RemoteLifecycleCustody,
        flavor_selectors: tuple[str, ...],
        target_profile: str,
        model_selector: str | None,
        accepted_source_only: bool,
        accepted_source_provider_id: str | None,
        accepted_source_provider_identity: ContentIdentity | None,
        jobs: int,
    ) -> dict[str, object]: ...


def _file_identity(path: Path) -> ContentIdentity:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return ContentIdentity(HashAlgorithm.SHA256, digest.hexdigest())


def _directory_identity(path: Path) -> ContentIdentity:
    entries: list[dict[str, object]] = []
    for item in sorted(
        path.rglob("*"), key=lambda candidate: candidate.relative_to(path).as_posix()
    ):
        if item.is_symlink() or (not item.is_file() and not item.is_dir()):
            raise RemoteExecutionError(
                "execution.remote_artifact_invalid",
                "accepted artifact contains an unsupported entry",
            )
        if item.is_file():
            entries.append(
                {
                    "path": item.relative_to(path).as_posix(),
                    "identity": _file_identity(item).uri,
                    "executable": bool(item.stat().st_mode & 0o111),
                }
            )
    return canonical_identity(
        {"schema": "literate-ai/worker-artifact-directory@1", "entries": entries}
    )


def _artifact_identity(path: Path) -> ContentIdentity:
    return _file_identity(path) if path.is_file() else _directory_identity(path)


def _artifact_reference(
    identity: ContentIdentity, execution_identity: ContentIdentity | None = None
) -> ContentReference:
    locator = identity.uri
    if execution_identity is not None:
        locator = f"{locator}/execution/{execution_identity.uri}"
    return ContentReference("artifact-export", f"{_CAS_SCHEME}:{locator}", identity)


def _cas_entry(
    root: Path,
    identity: ContentIdentity,
    execution_identity: ContentIdentity | None = None,
) -> Path:
    base = root / "artifacts" / identity.digest[:2] / identity.digest
    return base if execution_identity is None else base / execution_identity.digest


def _execution_manifest_identity(
    artifact_identity: ContentIdentity,
    argv: tuple[str, ...],
    environment: dict[str, str],
    toolchain_identities: tuple[ContentIdentity, ...],
    entrypoints: tuple[ArtifactEntrypointCommand, ...],
    *,
    artifact_path: Path,
) -> ContentIdentity:
    """Bind dispatch commands without binding a worker-private CAS pathname."""

    original = str(artifact_path)

    def portable(value: str) -> str:
        return value.replace(original, "{artifact}")

    command: dict[str, object] = {
        "argv": [portable(item) for item in argv],
        "environment": {
            name: portable(value) for name, value in sorted(environment.items())
        },
        "toolchain_identities": [item.to_dict() for item in toolchain_identities],
    }
    if entrypoints:
        command["entrypoints"] = [
            {
                "schema": item.to_dict()["schema"],
                "name": item.name,
                "kind": item.kind,
                "deployment_unit": item.deployment_unit,
                "argv": [portable(argument) for argument in item.argv],
                "environment": {
                    name: portable(value)
                    for name, value in sorted(item.environment.items())
                },
            }
            for item in entrypoints
        ]
        command["default_entrypoint"] = entrypoints[0].name
    return canonical_identity(
        {
            "schema": "literate-ai/worker-artifact-execution@1",
            "artifact_identity": artifact_identity.to_dict(),
            "command": command,
        }
    )


def _redact_diagnostic(value: object, *, limit: int = 8192) -> str:
    """Apply the explicit remote diagnostic redaction policy before transport."""

    text = str(value).replace("\x00", "")
    text = _SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text
    )
    text = _WINDOWS_PATH.sub("<private-path>", text)
    text = _POSIX_PATH.sub("<private-path>", text)
    return text[:limit] or "remote lifecycle failed without a public diagnostic"


def _failure_diagnostic(exc: Exception) -> RemoteFailureDiagnostic:
    cause = exc.__cause__
    stdout = getattr(exc, "stdout", None)
    stderr = getattr(exc, "stderr", None)
    if cause is not None:
        stdout = stdout or getattr(cause, "stdout", None)
        stderr = stderr or getattr(cause, "stderr", None)
    return RemoteFailureDiagnostic(
        code=_redact_diagnostic(
            getattr(exc, "code", "execution.remote_lifecycle_failed"), limit=256
        ),
        message=_redact_diagnostic(getattr(exc, "message", exc)),
        nested_code=(
            None
            if cause is None
            else _redact_diagnostic(
                getattr(cause, "code", type(cause).__name__), limit=256
            )
        ),
        nested_message=(
            None
            if cause is None
            else _redact_diagnostic(getattr(cause, "message", cause))
        ),
        stdout="" if stdout is None else _redact_diagnostic(stdout, limit=65536),
        stderr="" if stderr is None else _redact_diagnostic(stderr, limit=65536),
    )


def remote_control_summary(result: ExecutionDispatchResult) -> str:
    """Project one bounded redacted status summary into the SSH control channel."""

    selected = (
        result.stdout if result.status is DispatchResultStatus.PASSED else result.stderr
    )
    if not selected:
        return ""
    return _redact_diagnostic(selected, limit=MAX_REMOTE_CONTROL_SUMMARY_BYTES)


def _portable_result(value: object) -> object:
    """Project lifecycle output without worker-private paths or credential material."""

    if isinstance(value, dict):
        projected: dict[str, object] = {}
        for key, item in value.items():
            if key in {"artifact", "candidate_receipt", "runtime_root"}:
                continue
            projected[str(key)] = _portable_result(item)
        return projected
    if isinstance(value, list):
        return [_portable_result(item) for item in value]
    if isinstance(value, tuple):
        return [_portable_result(item) for item in value]
    if isinstance(value, str):
        return _redact_diagnostic(value, limit=65536)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RemoteExecutionError(
                "execution.remote_result_noncanonical",
                "worker lifecycle output contains a non-finite floating-point value",
            )
        decimal = Decimal(str(value))
        return {
            "encoding": "decimal-v1",
            "value": "0" if decimal.is_zero() else format(decimal.normalize(), "f"),
        }
    if isinstance(value, (bool, int)) or value is None:
        return value
    return _redact_diagnostic(value)


def _stage_identities(value: object) -> tuple[ContentIdentity, ...]:
    identities: set[ContentIdentity] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item):
            identities.add(ContentIdentity.parse_uri(item))

    visit(value)
    return tuple(sorted(identities, key=lambda item: item.uri))


def _evidence_kind(relative: str) -> str:
    first = relative.split("/", 1)[0]
    if first in {"source", "object", "binary", "package", "test", "receipt", "log"}:
        return first
    if first == "artifact":
        return "artifact"
    if first == "diagnostic":
        return "diagnostic"
    return "manifest"


def _copy_evidence_tree(
    source: Path,
    destination: Path,
    prefix: str,
    *,
    files: list[RemoteEvidenceFile],
    totals: list[int],
) -> None:
    if not source.exists():
        return
    candidates = (source,) if source.is_file() else tuple(sorted(source.rglob("*")))
    for item in candidates:
        if item.is_dir():
            continue
        if item.is_symlink() or not item.is_file():
            raise RemoteExecutionError(
                "execution.remote_evidence_entry_invalid",
                "remote lifecycle evidence contains an unsupported entry",
            )
        suffix = item.name if source.is_file() else item.relative_to(source).as_posix()
        relative = f"{prefix}/{suffix}"
        if len(files) >= _MAX_EVIDENCE_FILES:
            raise RemoteExecutionError(
                "execution.remote_evidence_too_large",
                "remote lifecycle evidence contains too many files",
            )
        size = item.stat().st_size
        totals[0] += size
        if totals[0] > _MAX_EVIDENCE_BYTES:
            raise RemoteExecutionError(
                "execution.remote_evidence_too_large",
                "remote lifecycle evidence exceeds the eight GiB custody limit",
            )
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target)
        executable = bool(item.stat().st_mode & 0o111)
        if executable:
            target.chmod(target.stat().st_mode | 0o111)
        files.append(
            RemoteEvidenceFile(
                relative,
                _evidence_kind(relative),
                size,
                _file_identity(target),
                executable,
            )
        )


def _write_deterministic_tar_gz(source: Path, destination: Path) -> ContentIdentity:
    temporary = destination.with_name(destination.name + ".partial")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        temporary.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for item in sorted(source.rglob("*")):
            if not item.is_file() or item.is_symlink():
                continue
            info = tarfile.TarInfo(item.relative_to(source).as_posix())
            info.size = item.stat().st_size
            info.mode = 0o755 if item.stat().st_mode & 0o111 else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with item.open("rb") as stream:
                archive.addfile(info, stream)
    identity = _file_identity(temporary)
    os.replace(temporary, destination)
    return identity


def _write_remote_evidence(
    *,
    worker: ExecutionWorker,
    request: ExecutionDispatchRequest,
    materialization: ExecutionSourceMaterialization,
    result: ExecutionDispatchResult,
    lifecycle_result: dict[str, object] | None,
    failure: RemoteFailureDiagnostic | None,
    custody: RemoteLifecycleCustody | None,
    artifact: Path | None,
    output: Path,
    cleanup_token: str,
) -> tuple[RemoteLifecycleEvidenceManifest, ContentReference]:
    temporary_root = Path(resolve_host_paths().temporary_root)
    prepare_user_directory(temporary_root, temporary_root)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".litai-e-{cleanup_token[:8]}-",
            dir=temporary_root,
        )
    )
    files: list[RemoteEvidenceFile] = []
    totals = [0]
    try:
        portable_result = _portable_result(lifecycle_result or {})
        result_path = staging / "manifest" / "lifecycle-result.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_bytes(canonical_json_bytes(portable_result))
        files.append(
            RemoteEvidenceFile(
                "manifest/lifecycle-result.json",
                "manifest",
                result_path.stat().st_size,
                _file_identity(result_path),
            )
        )
        if failure is not None:
            diagnostic_path = staging / "diagnostic" / "failure.json"
            diagnostic_path.parent.mkdir(parents=True)
            diagnostic_path.write_bytes(canonical_json_bytes(failure.to_dict()))
            files.append(
                RemoteEvidenceFile(
                    "diagnostic/failure.json",
                    "diagnostic",
                    diagnostic_path.stat().st_size,
                    _file_identity(diagnostic_path),
                )
            )
        for name, value in (("stdout", result.stdout), ("stderr", result.stderr)):
            if not value:
                continue
            log_path = staging / "log" / f"{name}.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                _redact_diagnostic(value, limit=65536),
                encoding="utf-8",
                newline="\n",
            )
            files.append(
                RemoteEvidenceFile(
                    f"log/{name}.txt",
                    "log",
                    log_path.stat().st_size,
                    _file_identity(log_path),
                )
            )
        if custody is not None:
            _copy_evidence_tree(
                custody.candidate_receipt,
                staging,
                "receipt",
                files=files,
                totals=totals,
            )
            _copy_evidence_tree(
                custody.runtime_root,
                staging,
                "source/runtime",
                files=files,
                totals=totals,
            )
            _copy_evidence_tree(
                custody.cache_directories.obj_dir,
                staging,
                "object/cache",
                files=files,
                totals=totals,
            )
            _copy_evidence_tree(
                custody.cache_directories.build_dir,
                staging,
                "source/cache",
                files=files,
                totals=totals,
            )
        if artifact is not None:
            _copy_evidence_tree(
                artifact,
                staging,
                "artifact",
                files=files,
                totals=totals,
            )
        if result.library_product is not None:
            if artifact is None:
                raise RemoteExecutionError(
                    "execution.remote_library_invalid", "library package is missing"
                )
            require_library_package(result.library_product, artifact)
            package = staging / "package" / "library.zip"
            package.parent.mkdir()
            package.write_bytes(directory_export_bytes(artifact))
            totals[0] += package.stat().st_size
            if len(files) >= _MAX_EVIDENCE_FILES or totals[0] > _MAX_EVIDENCE_BYTES:
                raise RemoteExecutionError(
                    "execution.remote_evidence_too_large",
                    "sealed library package exceeds evidence custody limits",
                )
            files.append(
                RemoteEvidenceFile(
                    "package/library.zip",
                    "package",
                    package.stat().st_size,
                    _file_identity(package),
                )
            )
        files.sort(key=lambda item: item.path)
        manifest = RemoteLifecycleEvidenceManifest(
            request.identity,
            request.authority_identity,
            worker.identity,
            materialization.identity,
            request.source_identity,
            request.specification_identity,
            request.flavor_identity,
            request.toolchain_identity,
            request.source_index_identity,
            request.model_scope_identity,
            request.action.value,
            result.status.value,
            result.evidence_identity,
            canonical_identity(result.observed_environment.to_dict()),
            (
                None
                if artifact is None or result.artifact_reference is None
                else result.artifact_reference.identity
            ),
            None if artifact is None else artifact.is_dir(),
            _stage_identities(lifecycle_result or {}),
            tuple(files),
            failure,
            cleanup_token,
            library_product=result.library_product,
        )
        (staging / "remote-evidence-manifest.json").write_bytes(
            canonical_json_bytes(manifest.to_dict())
        )
        bundle_identity = _write_deterministic_tar_gz(staging, output)
        return manifest, ContentReference(
            "remote-lifecycle-evidence", _EVIDENCE_URI, bundle_identity
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _persist_library_artifact(
    artifact: Path,
    product: LibraryArtifactProduct,
    cas_root: Path,
    toolchain_identities: tuple[ContentIdentity, ...],
) -> ContentReference:
    """Retain the exact sealed package under a non-executable CAS locator.

    This is package custody, not import authorization. Dispatch/evidence transport
    must separately bind this product before presenting it to a consumer.
    """

    temporary: Path | None = None
    try:
        require_library_package(product, artifact)
        identity = _artifact_identity(artifact)
        value = library_worker_manifest(identity, product, toolchain_identities)
        manifest_identity = canonical_identity(value)
        destination = _cas_entry(cas_root, identity, manifest_identity)
        reference = ContentReference(
            "artifact-export",
            f"{_CAS_SCHEME}:{identity.uri}/library/{manifest_identity.uri}",
            identity,
        )
        ensure_safe_directory(destination.parent)
        if not destination.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".lib-", dir=destination.parent))
            copied = temporary / "payload"
            shutil.copytree(artifact, copied)
            require_library_package(product, copied)
            if _artifact_identity(copied) != identity:
                raise ValueError("worker library copy changed during retention")
            (temporary / "library.json").write_bytes(canonical_json_bytes(value))
            try:
                os.replace(temporary, destination)
                temporary = None
            except OSError:
                # A concurrent publisher may have won. Never replace or remove its
                # custody; the common resolver below must validate the winner.
                if not destination.exists():
                    raise
        _resolve_library_artifact(reference, cas_root)
        return reference
    except (OSError, ValueError, UnsafeFilesystemPathError) as exc:
        raise RemoteExecutionError(
            "execution.remote_library_invalid", str(exc)
        ) from exc
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def _resolve_library_artifact(
    reference: ContentReference, cas_root: Path
) -> tuple[Path, dict[str, Any]]:
    try:
        parsed = urlsplit(reference.uri)
        prefix = f"{reference.identity.uri}/library/"
        if (
            reference.kind != "artifact-export"
            or parsed.scheme != _CAS_SCHEME
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith(prefix)
        ):
            raise ValueError("worker library locator is invalid")
        manifest_identity = ContentIdentity.parse_uri(parsed.path.removeprefix(prefix))
        entry = _cas_entry(cas_root, reference.identity, manifest_identity)
        require_safe_directory(entry)
        manifest = entry / "library.json"
        if path_is_link_or_reparse(manifest) or not manifest.is_file():
            raise ValueError("worker library manifest must be a regular file")
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "artifact_identity",
            "library_artifact",
            "toolchain_identities",
        }:
            raise ValueError("worker library manifest fields are invalid")
        product = LibraryArtifactProduct.from_dict(value["library_artifact"])
        if not isinstance(value["toolchain_identities"], list):
            raise ValueError("worker library toolchain identities must be an array")
        toolchains = tuple(
            ContentIdentity.from_dict(item) for item in value["toolchain_identities"]
        )
        expected = library_worker_manifest(reference.identity, product, toolchains)
        if canonical_identity(value) != manifest_identity or value != expected:
            raise ValueError("worker library manifest changed after acceptance")
        payload = entry / "payload"
        require_library_package(product, payload)
        if _artifact_identity(payload) != reference.identity:
            raise ValueError("worker library payload differs from its CAS identity")
        return payload, value
    except (OSError, ValueError, TypeError, UnsafeFilesystemPathError) as exc:
        raise RemoteExecutionError(
            "execution.remote_library_invalid", str(exc)
        ) from exc


def _persist_artifact(
    artifact: Path,
    execution_command: dict[str, Any],
    cas_root: Path,
    toolchain_identities: tuple[ContentIdentity, ...],
    execution_entrypoints: tuple[dict[str, Any], ...] = (),
) -> ContentReference:
    try:
        source = Path(artifact).resolve(strict=True)
    except OSError as exc:
        raise RemoteExecutionError(
            "execution.remote_artifact_missing", "accepted artifact is unavailable"
        ) from exc
    if source.is_symlink() or not (source.is_file() or source.is_dir()):
        raise RemoteExecutionError(
            "execution.remote_artifact_invalid",
            "accepted artifact must be a regular file or directory",
        )
    identity = _artifact_identity(source)
    argv = tuple(str(item) for item in execution_command.get("argv") or ())
    if not argv:
        raise RemoteExecutionError(
            "execution.remote_command_missing",
            "accepted artifact has no execution command",
        )
    environment = {
        str(name): str(value)
        for name, value in (execution_command.get("environment") or {}).items()
    }
    try:
        entrypoints = tuple(
            ArtifactEntrypointCommand.from_dict(item) for item in execution_entrypoints
        )
    except ArtifactExportError as exc:
        raise RemoteExecutionError(exc.code, exc.message) from exc
    if entrypoints:
        names = tuple(item.name for item in entrypoints)
        if len(entrypoints) < 2 or len(set(names)) != len(names):
            raise RemoteExecutionError(
                "execution.remote_artifact_manifest_invalid",
                "entrypoint command extension requires unique multiple commands",
            )
        default = entrypoints[0]
        if default.argv != argv or default.environment != environment:
            raise RemoteExecutionError(
                "execution.remote_artifact_manifest_invalid",
                "default entrypoint differs from the top-level command",
            )
    execution_identity = _execution_manifest_identity(
        identity,
        argv,
        environment,
        toolchain_identities,
        entrypoints,
        artifact_path=source,
    )
    destination = _cas_entry(cas_root, identity, execution_identity)
    payload = destination / "payload"
    manifest = destination / "execution.json"
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{identity.digest}.", dir=destination.parent)
        )
        try:
            staged = temporary / "payload"
            if source.is_dir():
                shutil.copytree(source, staged)
            else:
                shutil.copy2(source, staged)
            original = str(source)
            stored_argv = tuple(
                str(payload)
                if item == original
                else item.replace(original, str(payload))
                for item in argv
            )
            stored_environment = {
                str(name): str(value).replace(original, str(payload))
                for name, value in environment.items()
            }
            stored_entrypoints = tuple(
                ArtifactEntrypointCommand(
                    item.name,
                    item.kind,
                    item.deployment_unit,
                    tuple(
                        str(payload)
                        if argument == original
                        else argument.replace(original, str(payload))
                        for argument in item.argv
                    ),
                    {
                        name: value.replace(original, str(payload))
                        for name, value in item.environment.items()
                    },
                )
                for item in entrypoints
            )
            manifest_value: dict[str, Any] = {
                "argv": list(stored_argv),
                "environment": stored_environment,
                "toolchain_identities": [
                    item.to_dict() for item in toolchain_identities
                ],
                "execution_identity": execution_identity.to_dict(),
            }
            if stored_entrypoints:
                default = stored_entrypoints[0]
                if (
                    default.argv != stored_argv
                    or default.environment != stored_environment
                ):
                    raise RemoteExecutionError(
                        "execution.remote_artifact_manifest_invalid",
                        "default entrypoint differs from the top-level command",
                    )
                manifest_value["entrypoints"] = [
                    item.to_dict() for item in stored_entrypoints
                ]
                manifest_value["default_entrypoint"] = default.name
            (temporary / "execution.json").write_text(
                json.dumps(
                    manifest_value,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    if (
        not payload.exists()
        or not manifest.is_file()
        or _artifact_identity(payload) != identity
    ):
        raise RemoteExecutionError(
            "execution.remote_artifact_corrupt",
            "worker artifact CAS entry does not match its identity",
        )
    reference = _artifact_reference(identity, execution_identity)
    _resolve_artifact(reference, cas_root)
    return reference


def _resolve_artifact(
    reference: ContentReference, cas_root: Path
) -> tuple[Path, dict[str, Any]]:
    parsed = urlsplit(reference.uri)
    if parsed.path.startswith(f"{reference.identity.uri}/library/"):
        return _resolve_library_artifact(reference, cas_root)
    execution_identity: ContentIdentity | None = None
    legacy_locator = parsed.path == reference.identity.uri
    prefix = f"{reference.identity.uri}/execution/"
    if parsed.scheme == _CAS_SCHEME and parsed.path.startswith(prefix):
        try:
            execution_identity = ContentIdentity.parse_uri(
                parsed.path.removeprefix(prefix)
            )
        except ValueError as exc:
            raise RemoteExecutionError(
                "execution.remote_artifact_reference_invalid",
                "worker-CAS execution locator is invalid",
            ) from exc
    elif parsed.scheme != _CAS_SCHEME or not legacy_locator:
        raise RemoteExecutionError(
            "execution.remote_artifact_reference_invalid",
            "run requires a worker-CAS artifact reference",
        )
    entry = _cas_entry(cas_root, reference.identity, execution_identity)
    payload = entry / "payload"
    manifest = entry / "execution.json"
    try:
        if _artifact_identity(payload) != reference.identity:
            raise RemoteExecutionError(
                "execution.remote_artifact_corrupt",
                "worker artifact CAS payload does not match its identity",
            )
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except RemoteExecutionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteExecutionError(
            "execution.remote_artifact_unavailable",
            "worker artifact CAS entry is unavailable",
        ) from exc
    required = {"argv", "environment", "toolchain_identities"}
    keys = set(value) if isinstance(value, dict) else set()
    extras = keys - required
    allowed_extras = (
        set(),
        {"execution_identity"},
        {"entrypoints", "default_entrypoint"},
        {"entrypoints", "default_entrypoint", "execution_identity"},
    )
    if (
        not isinstance(value, dict)
        or not required.issubset(keys)
        or extras not in allowed_extras
        or not isinstance(value["argv"], list)
        or not value["argv"]
        or any(not isinstance(item, str) or not item for item in value["argv"])
        or not isinstance(value["environment"], dict)
        or any(
            not isinstance(name, str) or not isinstance(item, str)
            for name, item in value["environment"].items()
        )
        or not isinstance(value["toolchain_identities"], list)
    ):
        raise RemoteExecutionError(
            "execution.remote_artifact_manifest_invalid",
            "worker artifact execution manifest is invalid",
        )
    parsed_entrypoints: tuple[ArtifactEntrypointCommand, ...] = ()
    if "entrypoints" in value:
        try:
            parsed_entrypoints = tuple(
                ArtifactEntrypointCommand.from_dict(item)
                for item in value["entrypoints"]
            )
            names = tuple(item.name for item in parsed_entrypoints)
            if (
                len(parsed_entrypoints) < 2
                or len(parsed_entrypoints) > 256
                or len(set(names)) != len(names)
                or value["default_entrypoint"] not in names
            ):
                raise ValueError
            default = next(
                item
                for item in parsed_entrypoints
                if item.name == value["default_entrypoint"]
            )
            if (
                list(default.argv) != value["argv"]
                or default.environment != value["environment"]
            ):
                raise ValueError
        except (
            ArtifactExportError,
            KeyError,
            StopIteration,
            TypeError,
            ValueError,
        ) as exc:
            raise RemoteExecutionError(
                "execution.remote_artifact_manifest_invalid",
                "worker artifact entrypoint commands are invalid",
            ) from exc
    if execution_identity is not None:
        try:
            recorded_execution_identity = ContentIdentity.from_dict(
                value["execution_identity"]
            )
            toolchain_identities = tuple(
                ContentIdentity.from_dict(item)
                for item in value["toolchain_identities"]
            )
            observed_execution_identity = _execution_manifest_identity(
                reference.identity,
                tuple(value["argv"]),
                dict(value["environment"]),
                toolchain_identities,
                parsed_entrypoints,
                artifact_path=payload,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteExecutionError(
                "execution.remote_artifact_manifest_invalid",
                "worker artifact execution identity is invalid",
            ) from exc
        if (
            recorded_execution_identity != execution_identity
            or observed_execution_identity != execution_identity
        ):
            raise RemoteExecutionError(
                "execution.remote_artifact_manifest_changed",
                "worker artifact execution manifest changed after acceptance",
            )
    elif "execution_identity" in value:
        raise RemoteExecutionError(
            "execution.remote_artifact_reference_invalid",
            "legacy worker-CAS locator cannot address an execution-bound manifest",
        )
    return payload, value


def _observed_environment(
    worker: ExecutionWorker, toolchains: tuple[ContentIdentity, ...]
) -> ObservedExecutionEnvironment:
    local = ExecutionWorker(
        "local-probe",
        ExecutionWorkerKind.LOCAL,
        requirements=worker.requirements,
    )
    observed = probe_worker_capabilities(local)
    nvidia = tuple(device for device in observed.gpus if device.vendor == "nvidia")
    first = nvidia[0] if nvidia else None
    capabilities = tuple(
        sorted({capability for device in nvidia for capability in device.capabilities})
    )
    return ObservedExecutionEnvironment(
        observed.os_family,
        observed.os_version,
        observed.cpu_architecture,
        observed.logical_cpu_cores,
        observed.memory_mib,
        None if first is None else first.vendor,
        None if first is None else first.model,
        len(nvidia),
        None
        if first is None
        else min(device.memory_mib or 0 for device in nvidia) or None,
        capabilities,
        tuple(sorted(toolchains, key=lambda item: item.uri)),
    )


def execute_remote_request(
    worker: ExecutionWorker,
    request: ExecutionDispatchRequest,
    materialization: ExecutionSourceMaterialization,
    *,
    project_root: Path,
    cas_root: Path,
    rebuild: RemoteLifecycleRebuilder,
    accepted_source_entries: tuple[dict[str, Any], ...] | None = None,
    custody: RemoteLifecycleCustody | None = None,
    retain_lifecycle_custody: bool = False,
) -> ExecutionDispatchResult:
    """Execute after transport extraction, retaining only immutable worker artifacts."""

    if worker.kind is not ExecutionWorkerKind.SSH:
        raise RemoteExecutionError(
            "execution.remote_worker_kind_invalid", "receiver requires an SSH worker"
        )
    if (
        request.worker_identity != worker.identity
        or request.target_profile != worker.target_profile
    ):
        raise RemoteExecutionError(
            "execution.remote_request_mismatch",
            "request does not bind the selected SSH worker",
        )
    if materialization.request_identity != request.identity:
        raise RemoteExecutionError(
            "execution.remote_materialization_mismatch",
            "source materialization binds a different request",
        )
    root = Path(project_root).resolve(strict=True)
    current_source_identity = (
        source_tree_identity(root)
        if accepted_source_entries is None
        else verify_materialized_source(root, accepted_source_entries)
    )
    if current_source_identity != materialization.source_tree_identity.uri:
        raise RemoteExecutionError(
            "execution.remote_source_mismatch",
            "materialized project tree does not match transport authority",
        )
    artifact_reference: ContentReference | None = None
    library_product: LibraryArtifactProduct | None = None
    stdout = ""
    stderr = ""
    exit_status = 0
    coverage_gaps: dict[str, object] | None = None
    observed_toolchains: tuple[ContentIdentity, ...]
    if request.action in {LifecycleDispatchAction.BUILD, LifecycleDispatchAction.TEST}:
        lifecycle_custody = custody or RemoteLifecycleCustody(
            root.parent,
            bind_cache_directories(
                root,
                build_dir=root / "generated",
                obj_dir=root / "_build",
            ),
            root.parent / "lifecycle-runtime",
            root.parent / "candidate-receipt.json",
        )
        result = rebuild(
            component_path=request.component_path,
            project_root=root,
            custody=lifecycle_custody,
            flavor_selectors=request.flavor_selectors,
            target_profile=request.target_profile,
            model_selector=request.model_selector,
            accepted_source_only=request.accepted_source_only,
            jobs=request.jobs,
            accepted_source_provider_id=request.accepted_source_provider_id,
            accepted_source_provider_identity=(
                request.accepted_source_provider_identity
            ),
        )
        try:
            observed_toolchains = tuple(
                ContentIdentity.parse_uri(str(item))
                for item in result["observed_toolchain_identities"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteExecutionError(
                "execution.remote_toolchain_observation_missing",
                "accepted lifecycle omitted its observed toolchain closure",
            ) from exc
        if not observed_toolchains:
            raise RemoteExecutionError(
                "execution.remote_toolchain_observation_missing",
                "accepted lifecycle reported an empty toolchain closure",
            )
        try:
            if "library_artifact" in result:
                try:
                    library_product = LibraryArtifactProduct.from_dict(
                        result["library_artifact"]
                    )
                    if (
                        result.get("passed") is not True
                        or result.get("execution_command")
                        or result.get("execution_entrypoints")
                    ):
                        raise ValueError(
                            "library must be accepted without an execution command"
                        )
                except (TypeError, ValueError) as exc:
                    raise RemoteExecutionError(
                        "execution.remote_library_invalid", str(exc)
                    ) from exc
                artifact_reference = _persist_library_artifact(
                    Path(str(result["artifact"])),
                    library_product,
                    cas_root,
                    observed_toolchains,
                )
            else:
                artifact_reference = _persist_artifact(
                    Path(str(result["artifact"])),
                    dict(result["execution_command"]),
                    cas_root,
                    observed_toolchains,
                    tuple(
                        dict(item) for item in result.get("execution_entrypoints", [])
                    ),
                )
        finally:
            runtime = result.get("runtime_root")
            if runtime:
                from literate_ai.adapters.coverage_gaps import (
                    scan_runtime_coverage_gaps,
                )

                report = scan_runtime_coverage_gaps(
                    root, request.component_path, runtime
                )
                if report is not None:
                    coverage_gaps = report.to_dict()
                if not retain_lifecycle_custody:
                    shutil.rmtree(str(runtime), ignore_errors=True)
    else:
        if request.artifact_reference is None:
            raise RemoteExecutionError(
                "execution.remote_artifact_reference_missing",
                "run request requires an exact worker artifact reference",
            )
        payload, command = _resolve_artifact(request.artifact_reference, cas_root)
        if "library_artifact" in command:
            raise RemoteExecutionError(
                "execution.remote_library_not_executable",
                "library artifacts have no process entrypoint; "
                "consume the exact import surface",
            )
        try:
            observed_toolchains = tuple(
                ContentIdentity.from_dict(item)
                for item in command["toolchain_identities"]
            )
        except (TypeError, ValueError) as exc:
            raise RemoteExecutionError(
                "execution.remote_artifact_manifest_invalid",
                "worker artifact toolchain evidence is invalid",
            ) from exc
        if not observed_toolchains:
            raise RemoteExecutionError(
                "execution.remote_artifact_manifest_invalid",
                "worker artifact toolchain evidence is empty",
            )
        selected_command = None
        if "entrypoints" in command:
            selected_name = request.entrypoint or command["default_entrypoint"]
            selected_command = next(
                (
                    item
                    for item in command["entrypoints"]
                    if item["name"] == selected_name
                ),
                None,
            )
            if selected_command is None:
                available = ", ".join(item["name"] for item in command["entrypoints"])
                raise RemoteExecutionError(
                    "execution.remote_entrypoint_unknown",
                    f"unknown entrypoint {selected_name!r}; available entrypoints: "
                    f"{available}",
                )
        elif request.entrypoint is not None:
            raise RemoteExecutionError(
                "execution.remote_entrypoint_unsupported",
                "worker artifact is single-entrypoint and has no named selector",
            )
        selected_argv = (
            command["argv"] if selected_command is None else selected_command["argv"]
        )
        selected_environment = (
            command["environment"]
            if selected_command is None
            else selected_command["environment"]
        )
        argv = [*selected_argv, *request.arguments]
        environment = inherited_verbose_environment(
            {**os.environ, **selected_environment}
        )
        command_cwd = payload if "entrypoints" in command else payload.parent
        trace_subprocess(argv, cwd=command_cwd, environment=environment)
        completed = run_with_tree_kill(
            argv,
            cwd=command_cwd,
            env=environment,
            text=True,
            timeout=request.timeout_seconds,
        )
        exit_status = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
        artifact_reference = request.artifact_reference
    status = (
        DispatchResultStatus.PASSED if exit_status == 0 else DispatchResultStatus.FAILED
    )
    observation = _observed_environment(worker, observed_toolchains)
    evidence = canonical_identity(
        {
            "schema": "literate-ai/ssh-execution-evidence@1",
            "request_identity": request.identity.uri,
            "materialization_identity": materialization.identity.uri,
            "artifact_reference": None
            if artifact_reference is None
            else artifact_reference.to_dict(),
            "observed_environment": observation.to_dict(),
            "exit_status": exit_status,
        }
    )
    return ExecutionDispatchResult(
        request.identity,
        worker.identity,
        materialization.identity.uri,
        status,
        observation,
        exit_status,
        artifact_reference,
        evidence,
        stdout,
        stderr,
        coverage_gaps=coverage_gaps,
        library_product=library_product,
    )


def materialize_and_execute(
    worker: ExecutionWorker,
    request: ExecutionDispatchRequest,
    materialization: ExecutionSourceMaterialization,
    *,
    archive: Path,
    workspace: Path,
    cas_root: Path,
    rebuild: RemoteLifecycleRebuilder,
    accepted_source_cache_archive: Path | None = None,
    evidence_output: Path | None = None,
    cleanup_ticket: Path | None = None,
) -> ExecutionDispatchResult:
    if (evidence_output is None) != (cleanup_ticket is None):
        raise RemoteExecutionError(
            "execution.remote_evidence_custody_invalid",
            "evidence output and cleanup ticket must appear together",
        )
    if materialization.archive_reference is None:
        raise RemoteExecutionError(
            "execution.remote_archive_required",
            "this receiver invocation requires staged archive materialization",
        )
    if _file_identity(archive) != materialization.archive_reference.identity:
        raise RemoteExecutionError(
            "execution.remote_archive_mismatch",
            "staged archive bytes do not match transport authority",
        )
    if (accepted_source_cache_archive is None) != (
        materialization.accepted_source_cache_reference is None
    ):
        raise RemoteExecutionError(
            "execution.remote_accepted_source_cache_mismatch",
            "staged accepted-source-cache archive does not match transport authority",
        )
    if (
        accepted_source_cache_archive is not None
        and materialization.accepted_source_cache_reference is not None
        and _file_identity(accepted_source_cache_archive)
        != materialization.accepted_source_cache_reference.identity
    ):
        raise RemoteExecutionError(
            "execution.remote_accepted_source_cache_mismatch",
            "staged accepted-source-cache bytes do not match transport authority",
        )
    destination = Path(workspace).resolve()
    if destination.exists() or destination.is_symlink():
        raise RemoteExecutionError(
            "execution.remote_workspace_exists",
            "remote execution workspace must not already exist",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _identity, accepted_source_entries = extract_source_archive_with_manifest(
        archive, destination, materialization.source_tree_identity.uri
    )
    deferred_cleanup = evidence_output is not None
    short_attempt: Path | None = None
    lifecycle_result: dict[str, object] | None = None
    lifecycle_custody: RemoteLifecycleCustody | None = None
    artifact: Path | None = None
    cleanup_token = uuid.uuid4().hex

    def recording_rebuild(**values: Any) -> dict[str, object]:
        nonlocal lifecycle_result, artifact
        lifecycle_result = rebuild(**values)
        candidate = lifecycle_result.get("artifact")
        artifact = None if candidate is None else Path(str(candidate))
        return lifecycle_result

    try:
        project_root = destination / "literate-ai"
        if accepted_source_cache_archive is not None:
            short_attempt = Path(
                tempfile.mkdtemp(prefix=f"litai-s-{cleanup_token[:8]}-")
            ).resolve(strict=True)
            lifecycle_custody = RemoteLifecycleCustody(
                short_attempt,
                bind_cache_directories(
                    project_root,
                    build_dir=short_attempt / "build",
                    obj_dir=short_attempt / "objects",
                ),
                short_attempt / "runtime",
                short_attempt / "candidate-receipt.json",
            )
            _restore_accepted_source_cache(
                lifecycle_custody.cache_directories.build_dir,
                accepted_source_cache_archive,
            )
        else:
            lifecycle_custody = RemoteLifecycleCustody(
                destination,
                bind_cache_directories(
                    project_root,
                    build_dir=project_root / "generated",
                    obj_dir=project_root / "_build",
                ),
                destination / "lifecycle-runtime",
                destination / "candidate-receipt.json",
            )
        try:
            result = execute_remote_request(
                worker,
                request,
                materialization,
                project_root=project_root,
                cas_root=cas_root,
                rebuild=recording_rebuild,
                accepted_source_entries=accepted_source_entries,
                custody=lifecycle_custody,
                retain_lifecycle_custody=deferred_cleanup,
            )
            failure = None
        except Exception as exc:
            if not deferred_cleanup:
                raise
            failure = _failure_diagnostic(exc)
            observed = _observed_environment(worker, (request.toolchain_identity,))
            evidence = canonical_identity(
                {
                    "schema": "literate-ai/ssh-execution-failure-evidence@1",
                    "request_identity": request.identity.uri,
                    "worker_identity": worker.identity.uri,
                    "materialization_identity": materialization.identity.uri,
                    "diagnostic_identity": failure.identity.uri,
                }
            )
            result = ExecutionDispatchResult(
                request.identity,
                worker.identity,
                materialization.identity.uri,
                DispatchResultStatus.FAILED,
                observed,
                1,
                None,
                evidence,
                "",
                failure.message,
                failure.identity.uri,
            )
        if evidence_output is None or cleanup_ticket is None:
            return result
        manifest, reference = _write_remote_evidence(
            worker=worker,
            request=request,
            materialization=materialization,
            result=result,
            lifecycle_result=lifecycle_result,
            failure=failure,
            custody=lifecycle_custody,
            artifact=artifact,
            output=Path(evidence_output),
            cleanup_token=cleanup_token,
        )
        ticket_value = {
            "schema": "literate-ai/remote-evidence-cleanup-ticket@1",
            "cleanup_token": cleanup_token,
            "request_identity": request.identity.uri,
            "manifest_identity": manifest.identity.uri,
            "bundle_identity": reference.identity.uri,
            "roots": [
                str(item) for item in (destination, short_attempt) if item is not None
            ],
        }
        ticket_path = Path(cleanup_ticket)
        ticket_path.parent.mkdir(parents=True, exist_ok=True)
        ticket_path.write_bytes(canonical_json_bytes(ticket_value))
        return replace(
            result,
            evidence_manifest=manifest,
            evidence_reference=reference,
        )
    finally:
        if not deferred_cleanup:
            if short_attempt is not None:
                shutil.rmtree(short_attempt, ignore_errors=True)
            shutil.rmtree(destination, ignore_errors=True)


def import_remote_evidence_bundle(
    bundle: Path,
    manifest: RemoteLifecycleEvidenceManifest,
    *,
    expected_bundle_identity: ContentIdentity,
    store_root: Path,
) -> tuple[ContentIdentity, tuple[ContentIdentity, ...]]:
    """Verify transported bytes, then deterministically import coordinator custody."""

    source = Path(bundle)
    if _file_identity(source) != expected_bundle_identity:
        raise RemoteExecutionError(
            "execution.remote_evidence_bundle_mismatch",
            "transported evidence bundle does not match its declared identity",
        )
    root = Path(store_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="litai-evidence-import-"))
    try:
        transported_modes: dict[str, bool] = {}
        with tarfile.open(source, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_EVIDENCE_FILES + 1:
                raise RemoteExecutionError(
                    "execution.remote_evidence_too_large",
                    "remote evidence bundle contains too many entries",
                )
            total = 0
            for member in members:
                parts = Path(member.name).parts
                if (
                    not member.isfile()
                    or member.name.startswith("/")
                    or "\\" in member.name
                    or not parts
                    or any(part in {"", ".", ".."} for part in parts)
                    or member.name in transported_modes
                ):
                    raise RemoteExecutionError(
                        "execution.remote_evidence_bundle_invalid",
                        "remote evidence bundle contains an unsafe entry",
                    )
                transported_modes[member.name] = bool(member.mode & 0o111)
                total += member.size
                if total > _MAX_EVIDENCE_BYTES:
                    raise RemoteExecutionError(
                        "execution.remote_evidence_too_large",
                        "remote evidence bundle exceeds the eight GiB custody limit",
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise RemoteExecutionError(
                        "execution.remote_evidence_bundle_invalid",
                        "remote evidence bundle entry is unreadable",
                    )
                target = temporary.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                if member.mode & 0o111:
                    target.chmod(target.stat().st_mode | 0o111)
        manifest_path = temporary / "remote-evidence-manifest.json"
        try:
            transported_manifest = RemoteLifecycleEvidenceManifest.from_dict(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise RemoteExecutionError(
                "execution.remote_evidence_manifest_invalid",
                "transported remote evidence manifest is unavailable or invalid",
            ) from exc
        if transported_manifest != manifest:
            raise RemoteExecutionError(
                "execution.remote_evidence_manifest_mismatch",
                "transported manifest differs from the signed result manifest",
            )
        imported: list[ContentIdentity] = []
        expected_paths = {"remote-evidence-manifest.json"}
        for item in manifest.files:
            expected_paths.add(item.path)
            candidate = temporary.joinpath(*item.path.split("/"))
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size != item.size
                or _file_identity(candidate) != item.identity
                or transported_modes.get(item.path) != item.executable
            ):
                raise RemoteExecutionError(
                    "execution.remote_evidence_file_mismatch",
                    f"transported evidence file {item.path!r} failed verification",
                )
            blob = (
                root
                / "blobs"
                / "sha256"
                / item.identity.digest[:2]
                / item.identity.digest
            )
            if not blob.exists():
                blob.parent.mkdir(parents=True, exist_ok=True)
                staged = blob.with_name(blob.name + f".{uuid.uuid4().hex}.partial")
                shutil.copyfile(candidate, staged)
                if item.executable:
                    staged.chmod(staged.stat().st_mode | 0o111)
                os.replace(staged, blob)
            if _file_identity(blob) != item.identity:
                raise RemoteExecutionError(
                    "execution.remote_evidence_cas_corrupt",
                    "coordinator evidence CAS contains conflicting bytes",
                )
            imported.append(item.identity)
        if manifest.action in {"build", "test"} and manifest.outcome == "passed":
            artifact_files = tuple(
                item for item in manifest.files if item.path.startswith("artifact/")
            )
            if manifest.artifact_identity is None or not artifact_files:
                raise RemoteExecutionError(
                    "execution.remote_evidence_artifact_missing",
                    "passing remote lifecycle evidence omitted its accepted artifact",
                )
            if manifest.artifact_is_directory:
                observed_artifact_identity = canonical_identity(
                    {
                        "schema": "literate-ai/worker-artifact-directory@1",
                        "entries": [
                            {
                                "path": item.path.removeprefix("artifact/"),
                                "identity": item.identity.uri,
                                "executable": item.executable,
                            }
                            for item in artifact_files
                        ],
                    }
                )
            elif len(artifact_files) == 1:
                observed_artifact_identity = artifact_files[0].identity
            else:
                raise RemoteExecutionError(
                    "execution.remote_evidence_artifact_invalid",
                    "single-file remote artifact has ambiguous transported custody",
                )
            if observed_artifact_identity != manifest.artifact_identity:
                raise RemoteExecutionError(
                    "execution.remote_evidence_artifact_mismatch",
                    "transported artifact bytes do not match the accepted artifact",
                )
            if manifest.library_product is not None:
                try:
                    require_transported_library_package(
                        manifest.library_product,
                        temporary / "package" / "library.zip",
                        temporary / "artifact",
                        executable_by_path={
                            item.path.removeprefix("artifact/"): item.executable
                            for item in artifact_files
                        },
                    )
                except (OSError, ValueError, zipfile.BadZipFile) as exc:
                    raise RemoteExecutionError(
                        "execution.remote_library_invalid", str(exc)
                    ) from exc
        actual_paths = {
            item.relative_to(temporary).as_posix()
            for item in temporary.rglob("*")
            if item.is_file()
        }
        if actual_paths != expected_paths:
            raise RemoteExecutionError(
                "execution.remote_evidence_bundle_unlisted",
                "remote evidence bundle contains missing or undeclared files",
            )
        manifest_destination = (
            root / "manifests" / "sha256" / f"{manifest.identity.digest}.json"
        )
        manifest_destination.parent.mkdir(parents=True, exist_ok=True)
        canonical_manifest = canonical_json_bytes(manifest.to_dict())
        if not manifest_destination.exists():
            staged_manifest = manifest_destination.with_name(
                manifest_destination.name + f".{uuid.uuid4().hex}.partial"
            )
            staged_manifest.write_bytes(canonical_manifest)
            os.replace(staged_manifest, manifest_destination)
        elif manifest_destination.read_bytes() != canonical_manifest:
            raise RemoteExecutionError(
                "execution.remote_evidence_manifest_collision",
                "coordinator evidence manifest CAS contains conflicting bytes",
            )
        imported_identities = tuple(sorted(set(imported), key=lambda item: item.uri))
        store_identity = canonical_identity(
            {
                "schema": "literate-ai/coordinator-evidence-import@1",
                "manifest_identity": manifest.identity.uri,
                "bundle_identity": expected_bundle_identity.uri,
                "file_identities": [item.uri for item in imported_identities],
            }
        )
        return store_identity, imported_identities
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def load_and_import_remote_evidence_bundle(
    bundle: Path,
    *,
    expected_manifest_identity: ContentIdentity,
    expected_manifest_size: int,
    expected_bundle_identity: ContentIdentity,
    expected_bundle_size: int,
    store_root: Path,
) -> tuple[
    RemoteLifecycleEvidenceManifest, ContentIdentity, tuple[ContentIdentity, ...]
]:
    """Verify compact-control sizes/digests, then import the referenced manifest."""

    source = Path(bundle)
    try:
        if (
            source.is_symlink()
            or not source.is_file()
            or source.stat().st_size != expected_bundle_size
        ):
            raise OSError
    except OSError as exc:
        raise RemoteExecutionError(
            "execution.remote_evidence_bundle_size_mismatch",
            "transported evidence bundle does not match its declared size",
        ) from exc
    if _file_identity(source) != expected_bundle_identity:
        raise RemoteExecutionError(
            "execution.remote_evidence_bundle_mismatch",
            "transported evidence bundle does not match its declared identity",
        )
    if (
        expected_manifest_size < 1
        or expected_manifest_size > MAX_REMOTE_EVIDENCE_MANIFEST_BYTES
    ):
        raise RemoteExecutionError(
            "execution.remote_evidence_manifest_size_invalid",
            "declared evidence manifest size exceeds its deterministic bound",
        )
    try:
        with tarfile.open(source, mode="r:gz") as archive:
            matches = [
                item
                for item in archive.getmembers()
                if item.name == "remote-evidence-manifest.json"
            ]
            if (
                len(matches) != 1
                or not matches[0].isfile()
                or matches[0].size != expected_manifest_size
            ):
                raise RemoteExecutionError(
                    "execution.remote_evidence_manifest_size_mismatch",
                    "transported evidence manifest does not match its declared size",
                )
            stream = archive.extractfile(matches[0])
            if stream is None:
                raise RemoteExecutionError(
                    "execution.remote_evidence_manifest_invalid",
                    "transported remote evidence manifest is unavailable",
                )
            manifest_bytes = stream.read(expected_manifest_size + 1)
    except RemoteExecutionError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise RemoteExecutionError(
            "execution.remote_evidence_bundle_invalid",
            "transported remote evidence bundle is unreadable",
        ) from exc
    observed_manifest_identity = ContentIdentity(
        HashAlgorithm.SHA256, hashlib.sha256(manifest_bytes).hexdigest()
    )
    if (
        len(manifest_bytes) != expected_manifest_size
        or observed_manifest_identity != expected_manifest_identity
    ):
        raise RemoteExecutionError(
            "execution.remote_evidence_manifest_mismatch",
            "transported evidence manifest does not match its declared identity",
        )
    try:
        manifest = RemoteLifecycleEvidenceManifest.from_dict(
            json.loads(manifest_bytes.decode("utf-8"))
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RemoteExecutionError(
            "execution.remote_evidence_manifest_invalid",
            "transported remote evidence manifest is invalid",
        ) from exc
    if (
        canonical_json_bytes(manifest.to_dict()) != manifest_bytes
        or manifest.identity != expected_manifest_identity
    ):
        raise RemoteExecutionError(
            "execution.remote_evidence_manifest_mismatch",
            "transported evidence manifest is not canonical control-bound content",
        )
    store_identity, imported = import_remote_evidence_bundle(
        source,
        manifest,
        expected_bundle_identity=expected_bundle_identity,
        store_root=store_root,
    )
    return manifest, store_identity, imported


def acknowledge_remote_evidence_cleanup(
    ticket: Path,
    *,
    manifest_identity: ContentIdentity,
    bundle_identity: ContentIdentity,
    acknowledgement_root: Path,
    retries: int = 3,
) -> ContentIdentity:
    """Delete ticket-bound attempt roots after coordinator acknowledgement."""

    ack_root = Path(acknowledgement_root).resolve()
    ack_root.mkdir(parents=True, exist_ok=True)
    ack_path = ack_root / f"{manifest_identity.digest}.json"
    if ack_path.is_file():
        try:
            value = json.loads(ack_path.read_text(encoding="utf-8"))
            acknowledgement = ContentIdentity.parse_uri(
                str(value["acknowledgement_identity"])
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ) as exc:
            raise RemoteExecutionError(
                "execution.remote_cleanup_acknowledgement_invalid",
                "stored remote cleanup acknowledgement is invalid",
            ) from exc
        if (
            value.get("schema")
            != "literate-ai/remote-evidence-cleanup-acknowledgement@1"
            or value.get("manifest_identity") != manifest_identity.uri
            or value.get("bundle_identity") != bundle_identity.uri
        ):
            raise RemoteExecutionError(
                "execution.remote_cleanup_acknowledgement_mismatch",
                "stored cleanup acknowledgement binds different evidence custody",
            )
        return acknowledgement
    try:
        value = json.loads(Path(ticket).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteExecutionError(
            "execution.remote_cleanup_ticket_invalid",
            "remote cleanup ticket is unavailable or invalid",
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "literate-ai/remote-evidence-cleanup-ticket@1"
        or value.get("manifest_identity") != manifest_identity.uri
        or value.get("bundle_identity") != bundle_identity.uri
        or not isinstance(value.get("request_identity"), str)
        or not isinstance(value.get("cleanup_token"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["cleanup_token"])
        or not isinstance(value.get("roots"), list)
    ):
        raise RemoteExecutionError(
            "execution.remote_cleanup_ticket_mismatch",
            "remote cleanup ticket does not bind the acknowledged custody transfer",
        )
    request = ContentIdentity.parse_uri(value["request_identity"])
    roots = tuple(Path(str(item)).resolve() for item in value["roots"])
    if not roots or len(roots) > 2 or len(set(roots)) != len(roots):
        raise RemoteExecutionError(
            "execution.remote_cleanup_ticket_invalid",
            "remote cleanup ticket names an invalid root set",
        )
    for root in roots:
        safe_workspace = root.name.startswith(request.digest[:24] + "-")
        safe_attempt = root.name.startswith(f"litai-s-{value['cleanup_token'][:8]}-")
        if root == Path(root.anchor) or not (safe_workspace or safe_attempt):
            raise RemoteExecutionError(
                "execution.remote_cleanup_root_unsafe",
                "remote cleanup ticket names a root outside attempt custody",
            )
    failures: list[str] = []
    for root in roots:
        for attempt in range(retries):
            try:
                shutil.rmtree(root)
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                if attempt + 1 == retries:
                    failures.append(_redact_diagnostic(exc))
                else:
                    __import__("time").sleep(0.05 * (attempt + 1))
    if failures:
        raise RemoteExecutionError(
            "execution.remote_cleanup_failed",
            "acknowledged remote custody cleanup failed after bounded retries: "
            + "; ".join(failures),
        )
    acknowledgement = canonical_identity(
        {
            "schema": "literate-ai/remote-evidence-cleanup-acknowledgement@1",
            "request_identity": request.uri,
            "manifest_identity": manifest_identity.uri,
            "bundle_identity": bundle_identity.uri,
            "cleanup_token": value["cleanup_token"],
            "status": "cleaned",
        }
    )
    ack_path.write_bytes(
        canonical_json_bytes(
            {
                "schema": "literate-ai/remote-evidence-cleanup-acknowledgement@1",
                "manifest_identity": manifest_identity.uri,
                "bundle_identity": bundle_identity.uri,
                "acknowledgement_identity": acknowledgement.uri,
            }
        )
    )
    Path(ticket).unlink(missing_ok=True)
    return acknowledgement


def _restore_accepted_source_cache(build_dir: Path, archive: Path) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    cache_root = build_dir / "accepted-source-cache"
    if cache_root.exists() or cache_root.is_symlink():
        shutil.rmtree(cache_root, ignore_errors=True)
    try:
        extract_accepted_source_cache_archive(archive, cache_root)
    except SourceGuardError as exc:
        raise RemoteExecutionError(
            "execution.remote_accepted_source_cache_invalid",
            "staged accepted-source-cache archive could not be restored",
        ) from exc


__all__ = [
    "RemoteExecutionError",
    "RemoteLifecycleRebuilder",
    "acknowledge_remote_evidence_cleanup",
    "execute_remote_request",
    "import_remote_evidence_bundle",
    "load_and_import_remote_evidence_bundle",
    "materialize_and_execute",
    "remote_control_summary",
]
