"""Bounded SSH transport for retained-harness execution.

This first retained-receipt remote slice transports only the admitted source
projection, its canonical inventory, and identity-bound control documents.  It
does not claim the full remote lifecycle evidence-bundle custody protocol.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
    read_directory_export,
)
from literate_ai.adapters.harness_inventory import (
    HARNESS_INVENTORY_SCHEMA,
    HarnessBaselineError,
    execute_retained_harness,
)
from literate_ai.adapters.harness_tree import observe_retained_tree
from literate_ai.adapters.remote_execution import _redact_diagnostic
from literate_ai.adapters.source_materialization import (
    SourceMaterializationError,
    write_bounded_source_archive,
)
from literate_ai.adapters.ssh_execution import (
    _Deadline,
    _posix_launcher_selection,
    _posix_path,
    _receiver_failure,
)
from literate_ai.adapters.ssh_transport import (
    MAX_SSH_OUTPUT_BYTES,
    BoundedSshProcessRunner,
    SshProcessRunner,
    SshTransportError,
    scp_arguments,
    ssh_arguments,
)
from literate_ai.contracts import (
    ContentIdentity,
    ExecutionWorker,
    ExecutionWorkerKind,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.diagnostics import redact_secrets
from literate_ai.remote_source_guard import (
    SourceGuardError,
    extract_source_archive_with_manifest,
)

RETAINED_REMOTE_REQUEST_SCHEMA = "literate-ai/retained-harness-remote-request@2"
RETAINED_REMOTE_RESULT_SCHEMA = "literate-ai/retained-harness-remote-result@1"
# Match the repository's finite host-install archive ceiling. This remains below the
# 8 GiB remote-evidence ceiling while admitting large retained source projections.
MAX_RETAINED_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_RETAINED_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_RETAINED_SOURCE_ENTRIES = 100_000
MAX_RETAINED_RUNTIME_FILE_BYTES = 32 * 1024 * 1024
MAX_RETAINED_RUNTIME_CONTENT_BYTES = 192 * 1024 * 1024
MAX_RETAINED_RUNTIME_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_RETAINED_RUNTIME_FILES = 20_000
MAX_RETAINED_RUNTIME_TOOL_BYTES = 64 * 1024 * 1024
MAX_RETAINED_CONTROL_BYTES = 1024 * 1024
MAX_RETAINED_INVENTORY_BYTES = 32 * 1024 * 1024
MAX_RETAINED_DIAGNOSTIC_CHARS = 32_768
MAX_RETAINED_PHASES = 128
_RETAINED_CLEANUP_TIMEOUT_SECONDS = 300
_RUNTIME_TOOLS_SCHEMA = "literate-ai/retained-runtime-tools@1"
_NINJA_REQUIREMENT = "ninja==1.13.2"
_UV_REQUIREMENT = "uv==0.12.12"
_LINUX_X86_64_TOOL_WHEELS = {
    _NINJA_REQUIREMENT: {
        "name": "ninja",
        "url": (
            "https://files.pythonhosted.org/packages/6e/53/"
            "ebfed7b689c338dd8ebeec9c0730c8d56821292f14e2536e5f3ef1a05744/"
            "ninja-1.13.2-py3-none-manylinux2014_x86_64."
            "manylinux_2_17_x86_64.whl"
        ),
        "sha256": ("65a24341b5ac09fcadcc37082660be40a94174e51a937fabf6e2cae26225fa2c"),
        "size": 183_365,
        "member": "ninja-1.13.2.data/scripts/ninja",
    },
    _UV_REQUIREMENT: {
        "name": "uv",
        "url": (
            "https://files.pythonhosted.org/packages/be/c2/"
            "e8ff20e5f0bf1011995688784bc71ed7edbc7ccd3836e4967895680b2968/"
            "uv-0.12.12-py3-none-manylinux_2_17_x86_64."
            "manylinux2014_x86_64.whl"
        ),
        "sha256": ("fa5df02fc619a3cc7a58810d6ffeb80ca1e01404b8ef7239bd1cf2103c02cacf"),
        "size": 20_041_994,
        "member": "uv-0.12.12.data/scripts/uv",
    },
}
_RECEIVER_ONLY_PYTHON_ENVIRONMENT = frozenset(
    {
        "PYTHONPATH",
        "PYTHONSAFEPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
    }
)
_PLATFORM_FIELDS = frozenset(
    {
        "operating_system",
        "operating_system_release",
        "machine",
        "python_implementation",
        "python_version",
    }
)


class RetainedHarnessRemoteError(RuntimeError):
    """Remote retained execution violated its bounded transport contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _retained_receiver_failure(completed: object) -> RetainedHarnessRemoteError:
    """Preserve one canonical receiver diagnostic after re-sanitizing it."""

    try:
        stderr = completed.stderr
        if not isinstance(stderr, bytes) or len(stderr) > MAX_SSH_OUTPUT_BYTES:
            raise ValueError
        lines = stderr.splitlines(keepends=True)
        if not lines:
            raise ValueError
        payload = lines[-1]
        envelope = json.loads(payload.decode("utf-8"))
        if (
            payload != canonical_json_bytes(envelope) + b"\n"
            or not isinstance(envelope, dict)
            or set(envelope) != {"command", "error", "ok", "schema"}
            or envelope.get("schema") != "literate-ai/cli-error@1"
            or envelope.get("ok") is not False
            or envelope.get("command") != "worker.execute-retained"
        ):
            raise ValueError
        error = envelope["error"]
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise ValueError
        code = error["code"]
        message = error["message"]
        if (
            not isinstance(code, str)
            or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", code) is None
            or not isinstance(message, str)
            or not message
            or len(message.encode("utf-8")) > MAX_RETAINED_DIAGNOSTIC_CHARS
            or any(ord(character) < 32 for character in message)
        ):
            raise ValueError
        return RetainedHarnessRemoteError(
            code,
            retained_remote_public_error_message(message),
        )
    except (
        AttributeError,
        KeyError,
        TypeError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        failure = _receiver_failure(completed)
        diagnostic = getattr(completed, "stderr", b"") or getattr(
            completed, "stdout", b""
        )
        if isinstance(diagnostic, bytes) and diagnostic:
            public = retained_remote_public_error_message(
                diagnostic[:MAX_SSH_OUTPUT_BYTES].decode("utf-8", errors="replace")
            )
            return RetainedHarnessRemoteError(
                failure.code,
                f"{failure.message}: {public}",
            )
        return RetainedHarnessRemoteError(failure.code, failure.message)


def retained_remote_public_error_message(value: object) -> str:
    """Return one bounded path- and secret-redacted worker error message."""

    return _redact_diagnostic(
        redact_secrets(str(value), os.environ),
        limit=MAX_RETAINED_DIAGNOSTIC_CHARS,
    )


def retained_harness_runtime_requirements(
    inventory: dict[str, object],
    *,
    source_root: Path | None = None,
) -> tuple[str, ...]:
    """Return pinned tools required by direct drivers or their admitted scripts."""

    stages = inventory.get("stages")
    if not isinstance(stages, list):
        return ()
    requirements: set[str] = set()
    first_tokens = {
        str(stage.get("command", "")).strip().split(None, 1)[0]
        for stage in stages
        if isinstance(stage, dict)
    }
    if "uv" in first_tokens:
        requirements.add(_UV_REQUIREMENT)
    if source_root is not None:
        evidence_paths = {
            stage.get("evidence")
            for stage in stages
            if isinstance(stage, dict) and isinstance(stage.get("evidence"), str)
        }
        source_scope = inventory.get("source_scope")
        scoped_paths = (
            source_scope.get("paths")
            if isinstance(source_scope, dict)
            and isinstance(source_scope.get("paths"), list)
            else []
        )
        evidence_paths.update(
            path
            for path in scoped_paths
            if isinstance(path, str)
            and (
                path.endswith((".sh", ".mk"))
                or PurePosixPath(path).name in {"Makefile", "makefile"}
            )
        )
        scanned_bytes = 0
        for evidence in sorted(evidence_paths):
            assert isinstance(evidence, str)
            relative = PurePosixPath(evidence)
            if (
                relative.is_absolute()
                or relative.as_posix() != evidence
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                continue
            path = Path(source_root).joinpath(*relative.parts)
            try:
                metadata = path.lstat()
                size = metadata.st_size
                if (
                    path_is_link_or_reparse(path)
                    or not stat.S_ISREG(metadata.st_mode)
                    or size > 1_048_576
                    or scanned_bytes + size > 8 * 1024 * 1024
                ):
                    continue
                content = path.read_bytes()
            except OSError:
                continue
            scanned_bytes += len(content)
            if b"uv " in content or b"uv\n" in content:
                requirements.add(_UV_REQUIREMENT)
            if b"-G Ninja" in content or b"ninja " in content:
                requirements.add(_NINJA_REQUIREMENT)
    return tuple(sorted(requirements))


def _download_runtime_tool(descriptor: dict[str, object]) -> bytes:
    """Fetch one pinned Linux tool wheel, then return its exact verified binary."""

    try:
        with urllib.request.urlopen(str(descriptor["url"]), timeout=60) as response:
            if (
                response.status != 200
                or response.geturl() != descriptor["url"]
                or response.headers.get("Content-Length") != str(descriptor["size"])
            ):
                raise OSError
            wheel = response.read(int(descriptor["size"]) + 1)
        if (
            len(wheel) != descriptor["size"]
            or hashlib.sha256(wheel).hexdigest() != descriptor["sha256"]
        ):
            raise OSError
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            members = [
                member
                for member in archive.infolist()
                if member.filename == descriptor["member"]
            ]
            if len(members) != 1:
                raise OSError
            member = members[0]
            if (
                member.orig_filename != member.filename
                or not 1 <= member.file_size <= MAX_RETAINED_RUNTIME_TOOL_BYTES
                or member.compress_size > len(wheel)
            ):
                raise OSError
            binary = archive.read(member)
        if len(binary) != member.file_size:
            raise OSError
        return binary
    except (
        OSError,
        urllib.error.URLError,
        ValueError,
        zipfile.BadZipFile,
    ) as exc:
        raise RetainedHarnessRemoteError(
            "retained_receipt.runtime_dependency_unavailable",
            "a pinned Linux runtime dependency could not be acquired or verified",
        ) from exc


def _runtime_tool_files(
    requirements: tuple[str, ...],
) -> tuple[DirectoryExportFile, ...]:
    if not requirements:
        return ()
    if any(
        requirement not in _LINUX_X86_64_TOOL_WHEELS for requirement in requirements
    ):
        raise RetainedHarnessRemoteError(
            "retained_receipt.runtime_dependency_unsupported",
            "the retained runtime dependency closure is unsupported",
        )
    artifacts = []
    binaries = []
    for requirement in requirements:
        descriptor = _LINUX_X86_64_TOOL_WHEELS[requirement]
        binary = _download_runtime_tool(descriptor)
        name = str(descriptor["name"])
        path = f"literate_ai_tools/x86_64/{name}"
        artifacts.append(
            {
                "architecture": "x86_64",
                "name": name,
                "path": path,
                "sha256": hashlib.sha256(binary).hexdigest(),
                "size": len(binary),
            }
        )
        binaries.append(DirectoryExportFile(path, binary, 0o755))
    manifest = {
        "schema": _RUNTIME_TOOLS_SCHEMA,
        "requirements": list(requirements),
        "artifacts": artifacts,
    }
    return (
        DirectoryExportFile(
            "literate_ai_tools/manifest.json",
            canonical_json_bytes(manifest) + b"\n",
            0o644,
        ),
        *binaries,
    )


def _identity(value: object, label: str) -> ContentIdentity:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an identity")
    return ContentIdentity.parse_uri(value)


def _text(value: object, label: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be bounded text")
    return value


def _platform_observation() -> dict[str, str]:
    return {
        "operating_system": platform.system().casefold(),
        "operating_system_release": platform.release(),
        "machine": platform.machine().casefold(),
        "python_implementation": platform.python_implementation().casefold(),
        "python_version": platform.python_version(),
    }


def _validated_platform(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _PLATFORM_FIELDS:
        raise ValueError("remote platform has an invalid shape")
    return {
        key: _text(value[key], f"platform.{key}", maximum=1024)
        for key in sorted(_PLATFORM_FIELDS)
    }


def _bounded_json(value: object, *, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("remote phase nesting is too deep")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 65536 or "\x00" in value:
            raise ValueError("remote phase string is invalid")
        return
    if isinstance(value, list):
        if len(value) > 4096:
            raise ValueError("remote phase list is too large")
        for item in value:
            _bounded_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 256 or any(
            not isinstance(key, str) or not key or len(key) > 128 for key in value
        ):
            raise ValueError("remote phase object is invalid")
        for item in value.values():
            _bounded_json(item, depth=depth + 1)
        return
    raise ValueError("remote phase contains a non-JSON value")


def _validated_phases(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_RETAINED_PHASES:
        raise ValueError("remote result must contain bounded phase evidence")
    phases: list[dict[str, object]] = []
    for phase in value:
        if not isinstance(phase, dict):
            raise ValueError("remote phase must be an object")
        if {"stdout_excerpt", "stderr_excerpt"} & set(phase):
            raise ValueError("remote phase contains unsanitized diagnostics")
        if (
            not isinstance(phase.get("phase"), str)
            or not isinstance(phase.get("command"), str)
            or type(phase.get("exit_code")) is not int
            or type(phase.get("timed_out")) is not bool
        ):
            raise ValueError("remote phase omits typed execution fields")
        _bounded_json(phase)
        phases.append(dict(phase))
    if len(canonical_json_bytes(phases)) > MAX_RETAINED_CONTROL_BYTES // 2:
        raise ValueError("remote phases exceed their control-result bound")
    return tuple(phases)


@dataclass(frozen=True, slots=True)
class RetainedHarnessRemoteRequest:
    project_id: str
    project_revision_identity: ContentIdentity
    worker_identity: ContentIdentity
    worker_catalog_identity: ContentIdentity
    runner_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    inventory_identity: ContentIdentity
    source_identity: ContentIdentity
    source_manifest_identity: ContentIdentity
    archive_identity: ContentIdentity
    archive_size: int
    runtime_archive_identity: ContentIdentity
    runtime_archive_size: int
    timeout_seconds: int

    def __post_init__(self) -> None:
        _text(self.project_id, "project_id", maximum=256)
        for value in (
            self.project_revision_identity,
            self.worker_identity,
            self.worker_catalog_identity,
            self.runner_identity,
            self.lifecycle_request_identity,
            self.inventory_identity,
            self.source_identity,
            self.source_manifest_identity,
            self.archive_identity,
            self.runtime_archive_identity,
        ):
            if not isinstance(value, ContentIdentity):
                raise TypeError("remote request identities must be typed")
        if (
            type(self.archive_size) is not int
            or not 1 <= self.archive_size <= MAX_RETAINED_ARCHIVE_BYTES
            or type(self.runtime_archive_size) is not int
            or not 1 <= self.runtime_archive_size <= MAX_RETAINED_RUNTIME_ARCHIVE_BYTES
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 86400
        ):
            raise ValueError("remote request bounds are invalid")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self._material())

    def _material(self) -> dict[str, object]:
        return {
            "schema": RETAINED_REMOTE_REQUEST_SCHEMA,
            "project_id": self.project_id,
            "project_revision_identity": self.project_revision_identity.uri,
            "worker_identity": self.worker_identity.uri,
            "worker_catalog_identity": self.worker_catalog_identity.uri,
            "runner_identity": self.runner_identity.uri,
            "lifecycle_request_identity": self.lifecycle_request_identity.uri,
            "inventory_identity": self.inventory_identity.uri,
            "source_identity": self.source_identity.uri,
            "source_manifest_identity": self.source_manifest_identity.uri,
            "archive_identity": self.archive_identity.uri,
            "archive_size": self.archive_size,
            "runtime_archive_identity": self.runtime_archive_identity.uri,
            "runtime_archive_size": self.runtime_archive_size,
            "timeout_seconds": self.timeout_seconds,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._material(), "identity": self.identity.uri}

    @classmethod
    def from_dict(cls, value: object) -> RetainedHarnessRemoteRequest:
        required = {
            "schema",
            "project_id",
            "project_revision_identity",
            "worker_identity",
            "worker_catalog_identity",
            "runner_identity",
            "lifecycle_request_identity",
            "inventory_identity",
            "source_identity",
            "source_manifest_identity",
            "archive_identity",
            "archive_size",
            "runtime_archive_identity",
            "runtime_archive_size",
            "timeout_seconds",
            "identity",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value.get("schema") != RETAINED_REMOTE_REQUEST_SCHEMA
        ):
            raise ValueError("remote request has an invalid shape")
        request = cls(
            _text(value["project_id"], "project_id", maximum=256),
            _identity(value["project_revision_identity"], "project revision"),
            _identity(value["worker_identity"], "worker"),
            _identity(value["worker_catalog_identity"], "worker catalog"),
            _identity(value["runner_identity"], "runner"),
            _identity(value["lifecycle_request_identity"], "lifecycle request"),
            _identity(value["inventory_identity"], "inventory"),
            _identity(value["source_identity"], "source"),
            _identity(value["source_manifest_identity"], "source manifest"),
            _identity(value["archive_identity"], "archive"),
            value["archive_size"],
            _identity(value["runtime_archive_identity"], "runtime archive"),
            value["runtime_archive_size"],
            value["timeout_seconds"],
        )
        if value["identity"] != request.identity.uri:
            raise ValueError("remote request identity does not match its content")
        return request


@dataclass(frozen=True, slots=True)
class RetainedHarnessRemoteResult:
    request_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    worker_identity: ContentIdentity
    inventory_identity: ContentIdentity
    source_identity: ContentIdentity
    platform: dict[str, str]
    phases: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        for value in (
            self.request_identity,
            self.lifecycle_request_identity,
            self.worker_identity,
            self.inventory_identity,
            self.source_identity,
        ):
            if not isinstance(value, ContentIdentity):
                raise TypeError("remote result identities must be typed")
        object.__setattr__(self, "platform", _validated_platform(self.platform))
        object.__setattr__(self, "phases", _validated_phases(list(self.phases)))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": RETAINED_REMOTE_RESULT_SCHEMA,
            "request_identity": self.request_identity.uri,
            "lifecycle_request_identity": self.lifecycle_request_identity.uri,
            "worker_identity": self.worker_identity.uri,
            "inventory_identity": self.inventory_identity.uri,
            "source_identity": self.source_identity.uri,
            "platform": dict(self.platform),
            "phases": [dict(item) for item in self.phases],
        }

    @classmethod
    def from_dict(cls, value: object) -> RetainedHarnessRemoteResult:
        required = {
            "schema",
            "request_identity",
            "lifecycle_request_identity",
            "worker_identity",
            "inventory_identity",
            "source_identity",
            "platform",
            "phases",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value.get("schema") != RETAINED_REMOTE_RESULT_SCHEMA
        ):
            raise ValueError("remote result has an invalid shape")
        return cls(
            _identity(value["request_identity"], "request"),
            _identity(value["lifecycle_request_identity"], "lifecycle request"),
            _identity(value["worker_identity"], "worker"),
            _identity(value["inventory_identity"], "inventory"),
            _identity(value["source_identity"], "source"),
            _validated_platform(value["platform"]),
            _validated_phases(value["phases"]),
        )


def _require_posix_ssh_worker(worker: ExecutionWorker) -> None:
    if worker.kind is not ExecutionWorkerKind.SSH:
        raise RetainedHarnessRemoteError(
            "retained_receipt.worker_kind_unsupported",
            "remote retained execution requires a configured SSH worker",
        )
    family = worker.requirements.os_family
    if family == "windows":
        raise RetainedHarnessRemoteError(
            "retained_receipt.worker_os_unsupported",
            "remote retained execution does not yet support Windows workers",
        )
    if family not in {"linux", "macos"}:
        raise RetainedHarnessRemoteError(
            "retained_receipt.worker_os_unsupported",
            "remote retained execution requires an explicit POSIX worker OS",
        )


def validate_retained_remote_result(
    result: RetainedHarnessRemoteResult,
    request: RetainedHarnessRemoteRequest,
    worker: ExecutionWorker,
) -> None:
    """Require all receiver-controlled identities and the actual OS to match."""

    if (
        result.request_identity != request.identity
        or result.lifecycle_request_identity != request.lifecycle_request_identity
        or result.worker_identity != worker.identity
        or result.inventory_identity != request.inventory_identity
        or result.source_identity != request.source_identity
    ):
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_identity_mismatch",
            "remote retained result does not bind the exact request, source, "
            "and worker",
        )
    expected_os = {"linux": "linux", "macos": "darwin"}[
        str(worker.requirements.os_family)
    ]
    if result.platform["operating_system"] != expected_os:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_platform_mismatch",
            "remote retained result reports a different operating system",
        )


def _source_archive(
    root: Path, inventory: dict[str, object], destination: Path
) -> tuple[ContentIdentity, ContentIdentity, int]:
    source_scope = inventory.get("source_scope")
    paths = source_scope.get("paths") if isinstance(source_scope, dict) else None
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= MAX_RETAINED_SOURCE_ENTRIES
        or any(not isinstance(item, str) for item in paths)
    ):
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_source_scope_invalid",
            "retained source scope is absent or exceeds remote transport bounds",
        )
    try:
        return write_bounded_source_archive(
            root,
            tuple(paths),
            destination,
            max_source_bytes=MAX_RETAINED_SOURCE_BYTES,
            max_archive_bytes=MAX_RETAINED_ARCHIVE_BYTES,
            max_entries=MAX_RETAINED_SOURCE_ENTRIES,
        )
    except SourceMaterializationError as exc:
        code = {
            "execution.source_archive_oversized": (
                "retained_receipt.remote_archive_oversized"
            ),
            "execution.source_entry_limit": (
                "retained_receipt.remote_source_scope_invalid"
            ),
            "execution.source_projection_oversized": (
                "retained_receipt.remote_source_oversized"
            ),
        }.get(exc.code, "retained_receipt.remote_source_invalid")
        raise RetainedHarnessRemoteError(
            code,
            exc.message,
        ) from exc


def _read_runtime_file(path: Path, metadata: os.stat_result) -> bytes:
    """Read one bounded regular package file without following substitutions."""

    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not 0 <= opened.st_size <= MAX_RETAINED_RUNTIME_FILE_BYTES
        ):
            raise OSError
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(opened.st_size + 1)
        after = path.lstat()
        if len(content) != opened.st_size or (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise OSError
        return content
    except OSError as exc:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "coordinator runtime package changed or could not be read safely",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _runtime_archive(
    package_root: Path,
    destination: Path,
    *,
    inventory: dict[str, object] | None = None,
    source_root: Path | None = None,
) -> tuple[ContentIdentity, int]:
    """Create one canonical bounded ZIP containing the coordinator package."""

    root = Path(package_root)
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "coordinator runtime package root is unavailable or unsafe",
        ) from exc
    if (
        root.name != "literate_ai"
        or path_is_link_or_reparse(root)
        or not stat.S_ISDIR(root_metadata.st_mode)
    ):
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "coordinator runtime package root is unavailable or unsafe",
        )
    selected: list[tuple[str, Path, os.stat_result]] = []
    total = 0
    try:
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path_is_link_or_reparse(path):
                raise OSError
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts:
                continue
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError
            if path.suffix.casefold() in {".pyc", ".pyo"}:
                continue
            if metadata.st_size > MAX_RETAINED_RUNTIME_FILE_BYTES:
                raise OSError
            total += metadata.st_size
            if (
                len(selected) >= MAX_RETAINED_RUNTIME_FILES
                or total > MAX_RETAINED_RUNTIME_CONTENT_BYTES
            ):
                raise OSError
            selected.append((f"literate_ai/{relative.as_posix()}", path, metadata))
    except (OSError, ValueError) as exc:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "coordinator runtime package violates its file or content bounds",
        ) from exc
    if not selected:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "coordinator runtime package contains no admissible files",
        )
    package_files = tuple(
        DirectoryExportFile(
            archive_path,
            _read_runtime_file(path, metadata),
            0o755 if metadata.st_mode & 0o111 else 0o644,
        )
        for archive_path, path, metadata in selected
    )
    tool_files = _runtime_tool_files(
        retained_harness_runtime_requirements(
            inventory or {},
            source_root=source_root,
        )
    )
    files = (*package_files, *tool_files)
    if sum(len(item.content) for item in files) > MAX_RETAINED_RUNTIME_CONTENT_BYTES:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "coordinator runtime package exceeds its content bound",
        )
    created = False
    try:
        content = encode_directory_export(
            files,
            max_bytes=MAX_RETAINED_RUNTIME_ARCHIVE_BYTES,
            max_entries=MAX_RETAINED_RUNTIME_FILES,
        )
        selected_destination = Path(destination)
        with selected_destination.open("xb") as stream:
            created = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, TypeError, ValueError) as exc:
        if created:
            Path(destination).unlink(missing_ok=True)
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "coordinator runtime archive could not be encoded within its bound",
        ) from exc
    return (
        ContentIdentity.parse_uri(f"sha256:{hashlib.sha256(content).hexdigest()}"),
        len(content),
    )


class RetainedHarnessSshExecutor:
    """Stage one retained projection and return a bounded typed receiver result."""

    def __init__(self, runner: SshProcessRunner | None = None) -> None:
        self.runner = runner or BoundedSshProcessRunner()

    def execute(
        self,
        worker: ExecutionWorker,
        *,
        project_id: str,
        project_revision_identity: ContentIdentity,
        worker_catalog_identity: ContentIdentity,
        runner_identity: ContentIdentity,
        lifecycle_request_identity: ContentIdentity,
        inventory: dict[str, object],
        inventory_bytes: bytes,
        source_root: Path,
        source_identity: ContentIdentity,
        timeout_seconds: int,
        cwd: Path,
    ) -> RetainedHarnessRemoteResult:
        _require_posix_ssh_worker(worker)
        assert worker.endpoint is not None and worker.workspace is not None
        inventory_identity = canonical_identity(inventory)
        if inventory_bytes != canonical_json_bytes(inventory):
            raise RetainedHarnessRemoteError(
                "retained_receipt.remote_inventory_invalid",
                "retained inventory bytes are not canonical",
            )
        deadline = _Deadline(time.monotonic() + timeout_seconds, timeout_seconds)
        with tempfile.TemporaryDirectory(prefix="litai-retained-ssh-") as directory:
            staging = Path(directory)
            archive_path = staging / "source.tar.gz"
            runtime_path = staging / "literate-ai-runtime.zip"
            try:
                source_manifest, archive_identity, archive_size = _source_archive(
                    source_root, inventory, archive_path
                )
            except (
                SourceGuardError,
                SourceMaterializationError,
                OSError,
                ValueError,
            ) as exc:
                raise RetainedHarnessRemoteError(
                    getattr(exc, "code", "retained_receipt.remote_source_invalid"),
                    retained_remote_public_error_message(
                        getattr(exc, "message", str(exc))
                    ),
                ) from exc
            runtime_identity, runtime_size = _runtime_archive(
                Path(__file__).absolute().parents[1],
                runtime_path,
                inventory=inventory,
                source_root=source_root,
            )
            request = RetainedHarnessRemoteRequest(
                project_id,
                project_revision_identity,
                worker.identity,
                worker_catalog_identity,
                runner_identity,
                lifecycle_request_identity,
                inventory_identity,
                source_identity,
                source_manifest,
                archive_identity,
                archive_size,
                runtime_identity,
                runtime_size,
                timeout_seconds,
            )
            request_path = staging / "request.json"
            inventory_path = staging / "inventory.json"
            worker_path = staging / "worker.json"
            request_path.write_bytes(canonical_json_bytes(request.to_dict()))
            inventory_path.write_bytes(inventory_bytes)
            worker_path.write_bytes(canonical_json_bytes(worker.to_dict()))
            run_key = f"{request.identity.digest[:24]}-{uuid.uuid4().hex[:12]}"
            incoming = f"{worker.workspace.rstrip('/')}/retained/{run_key}"
            workspace = f"{worker.workspace.rstrip('/')}/retained-work/{run_key}"
            primary: Exception | None = None
            completed = None
            attempt_owned = False
            try:
                self._run(
                    worker,
                    self._setup_command(incoming, workspace),
                    cwd,
                    deadline,
                )
                attempt_owned = True
                for source, name in (
                    (archive_path, "source.tar.gz"),
                    (runtime_path, "literate-ai-runtime.zip"),
                    (inventory_path, "inventory.json"),
                    (request_path, "request.json"),
                    (worker_path, "worker.json"),
                ):
                    self._run_argv(
                        scp_arguments(
                            source,
                            worker.endpoint,
                            f"{incoming}/{name}",
                            deadline.remaining(),
                        ),
                        cwd,
                        deadline,
                    )
                completed = self._run(
                    worker,
                    self._execute_command(worker, incoming, workspace),
                    cwd,
                    deadline,
                    require_success=False,
                )
            except Exception as exc:
                primary = exc
            if attempt_owned:
                cleanup_deadline = _Deadline(
                    time.monotonic() + _RETAINED_CLEANUP_TIMEOUT_SECONDS,
                    _RETAINED_CLEANUP_TIMEOUT_SECONDS,
                )
                try:
                    cleaned = self._run(
                        worker,
                        self._cleanup_command(incoming, workspace),
                        cwd,
                        cleanup_deadline,
                        require_success=False,
                    )
                    if cleaned.returncode != 0:
                        raise RetainedHarnessRemoteError(
                            "retained_receipt.remote_cleanup_failed",
                            "SSH worker did not clean the retained attempt directory",
                        )
                except Exception as exc:
                    raise RetainedHarnessRemoteError(
                        getattr(exc, "code", "retained_receipt.remote_cleanup_failed"),
                        getattr(
                            exc,
                            "message",
                            "SSH worker did not clean the retained attempt directory",
                        ),
                    ) from primary
            if primary is not None:
                raise primary
            assert completed is not None
            if completed.returncode != 0:
                raise _retained_receiver_failure(completed)
            try:
                if len(completed.stdout) > MAX_SSH_OUTPUT_BYTES:
                    raise ValueError
                envelope = json.loads(completed.stdout.decode("utf-8"))
                if completed.stdout != canonical_json_bytes(envelope) + b"\n":
                    raise ValueError
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema") != "literate-ai/cli-result@1"
                    or envelope.get("ok") is not True
                    or envelope.get("command") != "worker.execute-retained"
                ):
                    raise ValueError
                result = RetainedHarnessRemoteResult.from_dict(envelope["result"])
                validate_retained_remote_result(result, request, worker)
            except (
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                raise RetainedHarnessRemoteError(
                    "retained_receipt.remote_result_invalid",
                    "SSH retained receiver did not return one bounded canonical result",
                ) from exc
            return result

    def _run(
        self,
        worker: ExecutionWorker,
        command: str,
        cwd: Path,
        deadline: _Deadline,
        *,
        require_success: bool = True,
    ):
        assert worker.endpoint is not None
        return self._run_argv(
            ssh_arguments(
                worker.endpoint,
                command,
                deadline.remaining(),
                transport=worker.transport,
            ),
            cwd,
            deadline,
            require_success=require_success,
        )

    def _run_argv(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        deadline: _Deadline,
        *,
        require_success: bool = True,
    ):
        try:
            completed = self.runner.run(
                argv, cwd=cwd, timeout_seconds=deadline.remaining()
            )
        except SshTransportError as exc:
            raise RetainedHarnessRemoteError(exc.code, exc.message) from exc
        if require_success and completed.returncode != 0:
            raise RetainedHarnessRemoteError(
                "retained_receipt.remote_transport_failed",
                "SSH retained transport phase exited with status "
                f"{completed.returncode}",
            )
        return completed

    @staticmethod
    def _setup_command(incoming: str, workspace: str) -> str:
        return "; ".join(
            (
                "set -eu",
                f"incoming={_posix_path(incoming)}",
                f"workspace={_posix_path(workspace)}",
                'test ! -e "$incoming"',
                'test ! -e "$workspace"',
                'mkdir -p -- "${incoming%/*}" "${workspace%/*}"',
                'mkdir -- "$incoming"',
            )
        )

    @staticmethod
    def _execute_command(worker: ExecutionWorker, incoming: str, workspace: str) -> str:
        selection = _posix_launcher_selection(worker)
        return "; ".join(
            (
                "set -u",
                f"incoming={_posix_path(incoming)}",
                f"workspace={_posix_path(workspace)}",
                (
                    'PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"; '
                    "export PATH"
                ),
                selection,
                (
                    'PYTHONPATH="$incoming/literate-ai-runtime.zip"; '
                    "PYTHONSAFEPATH=1; PYTHONDONTWRITEBYTECODE=1; "
                    "PYTHONNOUSERSITE=1; LITAI_NO_SELF_UPDATE=1; "
                    "export PYTHONPATH PYTHONSAFEPATH PYTHONDONTWRITEBYTECODE "
                    "PYTHONNOUSERSITE LITAI_NO_SELF_UPDATE"
                ),
                (
                    '"$litai_command" worker execute-retained '
                    '--worker-file "$incoming/worker.json" '
                    '--request "$incoming/request.json" '
                    '--inventory "$incoming/inventory.json" '
                    '--archive "$incoming/source.tar.gz" '
                    '--runtime-archive "$incoming/literate-ai-runtime.zip" '
                    '--workspace "$workspace"'
                ),
                "status=$?",
                'exit "$status"',
            )
        )

    @staticmethod
    def _cleanup_command(incoming: str, workspace: str) -> str:
        return "; ".join(
            (
                "set -eu",
                f"incoming={_posix_path(incoming)}",
                f"workspace={_posix_path(workspace)}",
                'rm -rf -- "$incoming" "$workspace"',
                'test ! -e "$incoming"',
                'test ! -e "$workspace"',
            )
        )


def _read_canonical_json(path: Path, *, maximum: int, label: str) -> dict[str, object]:
    selected = Path(path)
    try:
        if selected.is_symlink() or not selected.is_file():
            raise OSError
        size = selected.stat().st_size
        if not 1 <= size <= maximum:
            raise OSError
        content = selected.read_bytes()
        value = json.loads(content.decode("utf-8"))
        if not isinstance(value, dict) or content != canonical_json_bytes(value):
            raise ValueError
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_control_invalid",
            f"{label} must be one bounded canonical JSON object",
        ) from exc


@contextmanager
def _without_receiver_python_environment():
    """Keep runtime-bridge Python controls out of retained child processes."""

    saved = {
        name: os.environ.pop(name)
        for name in _RECEIVER_ONLY_PYTHON_ENVIRONMENT
        if name in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(saved)


def _read_runtime_archive(
    path: Path,
    *,
    identity: ContentIdentity,
    size: int,
) -> tuple[DirectoryExportFile, ...]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != size
            or not 1 <= size <= MAX_RETAINED_RUNTIME_ARCHIVE_BYTES
        ):
            raise OSError
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_RETAINED_RUNTIME_ARCHIVE_BYTES + 1)
        if len(content) != size:
            raise OSError
        return read_directory_export(
            content,
            BlobRef(identity.digest, size),
            max_bytes=MAX_RETAINED_RUNTIME_ARCHIVE_BYTES,
            max_entries=MAX_RETAINED_RUNTIME_FILES,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "staged coordinator runtime archive is unavailable or invalid",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _with_retained_runtime_tools(
    runtime_archive: Path,
    *,
    request: RetainedHarnessRemoteRequest,
    inventory: dict[str, object],
    source_root: Path,
    workspace: Path,
):
    """Expose only identity-bound, inventory-required runtime tools to child gates."""

    requirements = retained_harness_runtime_requirements(
        inventory,
        source_root=source_root,
    )
    if not requirements:
        yield
        return
    files = _read_runtime_archive(
        runtime_archive,
        identity=request.runtime_archive_identity,
        size=request.runtime_archive_size,
    )
    by_path = {item.path: item for item in files}
    manifest_file = by_path.get("literate_ai_tools/manifest.json")
    try:
        if manifest_file is None or manifest_file.mode != 0o644:
            raise ValueError
        manifest = json.loads(manifest_file.content)
        if (
            manifest_file.content != canonical_json_bytes(manifest) + b"\n"
            or not isinstance(manifest, dict)
            or set(manifest) != {"schema", "requirements", "artifacts"}
            or manifest["schema"] != _RUNTIME_TOOLS_SCHEMA
            or manifest["requirements"] != list(requirements)
            or not isinstance(manifest["artifacts"], list)
            or not manifest["artifacts"]
        ):
            raise ValueError
        selected_architecture = platform.machine().casefold().replace("amd64", "x86_64")
        required_names = {
            str(_LINUX_X86_64_TOOL_WHEELS[requirement]["name"])
            for requirement in requirements
        }
        selected: dict[str, DirectoryExportFile] = {}
        admitted_paths = {"literate_ai_tools/manifest.json"}
        for artifact in manifest["artifacts"]:
            if (
                not isinstance(artifact, dict)
                or set(artifact) != {"architecture", "name", "path", "sha256", "size"}
                or artifact["name"] not in required_names
                or not isinstance(artifact["architecture"], str)
                or not isinstance(artifact["path"], str)
                or not isinstance(artifact["sha256"], str)
                or not isinstance(artifact["size"], int)
                or artifact["path"]
                != (f"literate_ai_tools/{artifact['architecture']}/{artifact['name']}")
            ):
                raise ValueError
            candidate = by_path.get(artifact["path"])
            if (
                candidate is None
                or candidate.mode != 0o755
                or not 1 <= artifact["size"] <= MAX_RETAINED_RUNTIME_TOOL_BYTES
                or len(candidate.content) != artifact["size"]
                or hashlib.sha256(candidate.content).hexdigest() != artifact["sha256"]
            ):
                raise ValueError
            admitted_paths.add(candidate.path)
            if artifact["architecture"] == selected_architecture:
                name = str(artifact["name"])
                if name in selected:
                    raise ValueError
                selected[name] = candidate
        if {
            path for path in by_path if path.startswith("literate_ai_tools/")
        } != admitted_paths:
            raise ValueError
        if set(selected) != required_names:
            raise RetainedHarnessRemoteError(
                "retained_receipt.runtime_dependency_unsupported",
                "the retained runtime dependency closure does not support this worker",
            )
    except RetainedHarnessRemoteError:
        raise
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "staged retained runtime dependency closure is invalid",
        ) from exc
    tools = workspace / ".literate-ai-tools"
    if tools.exists() or tools.is_symlink():
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "retained runtime tool destination is not empty",
        )
    bin_directory = tools / "bin"
    bin_directory.mkdir(parents=True)
    for name, artifact in sorted(selected.items()):
        executable = bin_directory / name
        with executable.open("xb") as stream:
            stream.write(artifact.content)
            stream.flush()
            os.fsync(stream.fileno())
        executable.chmod(0o755)
    previous = os.environ.get("PATH")
    os.environ["PATH"] = (
        os.fspath(bin_directory)
        if previous is None
        else f"{bin_directory}{os.pathsep}{previous}"
    )
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous


def _bounded_regular_file_sha256(
    path: Path,
    expected_size: int,
    *,
    maximum: int,
    code: str,
    message: str,
) -> str:
    """Hash one exact bounded non-symlink file without allocating it in memory."""

    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != expected_size
            or not 1 <= before.st_size <= maximum
        ):
            raise OSError
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while content := stream.read(1024 * 1024):
                size += len(content)
                digest.update(content)
        after = os.fstat(descriptor)
        if size != expected_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise OSError
        return digest.hexdigest()
    except OSError as exc:
        raise RetainedHarnessRemoteError(
            code,
            message,
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def execute_retained_harness_receiver(
    *,
    worker_file: Path,
    request_file: Path,
    inventory_file: Path,
    archive: Path,
    runtime_archive: Path,
    workspace: Path,
) -> RetainedHarnessRemoteResult:
    """Validate one staged attempt, execute it, and remove receiver workspace."""

    try:
        worker = ExecutionWorker.from_dict(
            _read_canonical_json(
                worker_file, maximum=MAX_RETAINED_CONTROL_BYTES, label="worker"
            )
        )
        request = RetainedHarnessRemoteRequest.from_dict(
            _read_canonical_json(
                request_file, maximum=MAX_RETAINED_CONTROL_BYTES, label="request"
            )
        )
        inventory = _read_canonical_json(
            inventory_file,
            maximum=MAX_RETAINED_INVENTORY_BYTES,
            label="retained inventory",
        )
    except (TypeError, ValueError) as exc:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_control_invalid",
            "retained receiver inputs violate their typed contract",
        ) from exc
    _require_posix_ssh_worker(worker)
    if request.worker_identity != worker.identity:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_identity_mismatch",
            "retained request binds a different worker",
        )
    if (
        inventory.get("schema") != HARNESS_INVENTORY_SCHEMA
        or canonical_identity(inventory) != request.inventory_identity
    ):
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_inventory_invalid",
            "staged retained inventory differs from the request",
        )
    runtime_digest = _bounded_regular_file_sha256(
        Path(runtime_archive),
        request.runtime_archive_size,
        maximum=MAX_RETAINED_RUNTIME_ARCHIVE_BYTES,
        code="retained_receipt.remote_runtime_invalid",
        message="staged coordinator runtime archive is unavailable or oversized",
    )
    if runtime_digest != request.runtime_archive_identity.digest:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_runtime_invalid",
            "staged coordinator runtime archive differs from the request",
        )
    selected_archive = Path(archive)
    digest = _bounded_regular_file_sha256(
        selected_archive,
        request.archive_size,
        maximum=MAX_RETAINED_ARCHIVE_BYTES,
        code="retained_receipt.remote_archive_invalid",
        message="staged retained source archive is unavailable or oversized",
    )
    if digest != request.archive_identity.digest:
        raise RetainedHarnessRemoteError(
            "retained_receipt.remote_archive_invalid",
            "staged retained source archive differs from the request",
        )
    destination = Path(workspace)
    try:
        _manifest, _entries = extract_source_archive_with_manifest(
            selected_archive,
            destination,
            request.source_manifest_identity.uri,
            max_archive_bytes=MAX_RETAINED_ARCHIVE_BYTES,
            max_entries=MAX_RETAINED_SOURCE_ENTRIES,
            max_total_bytes=MAX_RETAINED_SOURCE_BYTES,
        )
        source_root = destination / "literate-ai"
        source_observation = observe_retained_tree(
            source_root, inventory.get("source_scope")
        )["source_tree"]
        if source_observation.get("identity") != request.source_identity.uri:
            raise RetainedHarnessRemoteError(
                "retained_receipt.remote_source_mismatch",
                "materialized retained source differs from controller authority",
            )
        with (
            _without_receiver_python_environment(),
            _with_retained_runtime_tools(
                Path(runtime_archive),
                request=request,
                inventory=inventory,
                source_root=source_root,
                workspace=destination,
            ),
        ):
            report = execute_retained_harness(
                inventory,
                legacy_root=source_root,
                timeout_seconds=request.timeout_seconds,
            )
        phases = tuple(
            {
                key: value
                for key, value in phase.items()
                if key not in {"stdout_excerpt", "stderr_excerpt"}
            }
            for phase in report.get("phases", [])
            if isinstance(phase, dict)
        )
        return RetainedHarnessRemoteResult(
            request.identity,
            request.lifecycle_request_identity,
            worker.identity,
            request.inventory_identity,
            request.source_identity,
            _platform_observation(),
            phases,
        )
    except RetainedHarnessRemoteError:
        raise
    except (HarnessBaselineError, SourceGuardError, OSError, ValueError) as exc:
        raise RetainedHarnessRemoteError(
            getattr(exc, "code", "retained_receipt.remote_execution_failed"),
            getattr(exc, "message", "remote retained harness execution failed"),
        ) from exc
    finally:
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination, ignore_errors=True)


__all__ = [
    "MAX_RETAINED_ARCHIVE_BYTES",
    "MAX_RETAINED_CONTROL_BYTES",
    "MAX_RETAINED_DIAGNOSTIC_CHARS",
    "MAX_RETAINED_INVENTORY_BYTES",
    "MAX_RETAINED_RUNTIME_ARCHIVE_BYTES",
    "MAX_RETAINED_RUNTIME_CONTENT_BYTES",
    "MAX_RETAINED_RUNTIME_FILE_BYTES",
    "MAX_RETAINED_RUNTIME_FILES",
    "MAX_RETAINED_SOURCE_BYTES",
    "MAX_RETAINED_SOURCE_ENTRIES",
    "RETAINED_REMOTE_REQUEST_SCHEMA",
    "RETAINED_REMOTE_RESULT_SCHEMA",
    "RetainedHarnessRemoteError",
    "RetainedHarnessRemoteRequest",
    "RetainedHarnessRemoteResult",
    "RetainedHarnessSshExecutor",
    "execute_retained_harness_receiver",
    "retained_remote_public_error_message",
    "validate_retained_remote_result",
]
