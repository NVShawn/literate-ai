"""Authorization-enforcing execution of exact compiled sample artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
)
from literate_ai.adapters._processes import (
    terminate_process_tree as _terminate_process_tree,
)
from literate_ai.adapters.builders.javascript import NodeToolchain
from literate_ai.adapters.builders.python import BuildError
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.diagnostics import (
    inherited_verbose_environment,
    redact_secrets,
    trace_subprocess,
)
from literate_ai.evidence_ledger import EvidenceNode, attach_run
from literate_ai.security import (
    FailClosedObservationExecutionAuthorizationVerifier,
    ObservationExecutionAuthorization,
    ObservationExecutionAuthorizationVerifier,
    ObservationRequest,
)

from ._paths import canonical_relative_posix_path

AUTHORIZED_HOST_RUNNER_ID = "runner:authorized-host-artifact@1"
AUTHORIZED_HOST_PROCESS_PROFILE = "explicit-authorized-host-process@1"
AUTHORIZED_HOST_PRIVILEGES = (
    "compiler",
    "devices",
    "host-filesystem",
    "network",
    "package-manager",
    "processes",
    "sandbox-escape",
    "secrets",
)
DEFAULT_HOST_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_HOST_STDERR_LIMIT_BYTES = 1024 * 1024
_PROCESS_READ_CHUNK_BYTES = 64 * 1024
_PIPE_CLOSE_TIMEOUT_SECONDS = 5.0
_COMPILED_HOST_EXECUTION_MODES = frozenset({"cpp", "rust", "swift"})
_INTERPRETED_HOST_EXECUTION_MODES = frozenset(
    {"python", "javascript", "rust-javascript-full-stack"}
)
_HOST_EXECUTION_MODES = (
    _COMPILED_HOST_EXECUTION_MODES | _INTERPRETED_HOST_EXECUTION_MODES
)

_PYTHON_HOST_RUNNER = r"""
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

entrypoint = Path(sys.argv[1]).resolve(strict=True)
arguments = json.loads(sys.argv[2])
support_paths = json.loads(sys.argv[3])
if not isinstance(arguments, list) or not isinstance(support_paths, list):
    raise TypeError('host runner arguments must be arrays')
for support in reversed(support_paths):
    sys.path.insert(0, str(Path(support).resolve(strict=True)))
sys.path.insert(0, str(entrypoint.parent))
loader = importlib.machinery.SourcelessFileLoader('generated_sample', str(entrypoint))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None:
    raise RuntimeError('compiled module spec unavailable')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
loader.exec_module(module)
application = getattr(module, 'main', None)
if not callable(application):
    raise TypeError('compiled module must expose callable main')
result = application(*arguments)
output = {'module_file': str(Path(module.__file__).resolve()), 'result': result}
print(json.dumps(output, sort_keys=True, separators=(',', ':')))
"""


class HostArtifactExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        role: str | None = None,
        returncode: int | None = None,
        stdout_digest: str | None = None,
        stderr_digest: str | None = None,
        stdout: bytes | None = None,
        stderr: bytes | None = None,
    ) -> None:
        self.code = code
        self.role = role
        self.returncode = returncode
        self.stdout_digest = stdout_digest
        self.stderr_digest = stderr_digest
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(message)


def _digest_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _require_digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
    ):
        raise ValueError(f"{label} must be a sha256 digest")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a hexadecimal sha256 digest") from exc


def _relative_entrypoint(value: str) -> PurePosixPath:
    return canonical_relative_posix_path(value, label="host artifact entrypoint")


def _directory_digest(root: Path) -> str:
    resolved_root = root.resolve(strict=True)
    entries: list[dict[str, object]] = []
    for path in sorted(resolved_root.rglob("*")):
        if path.is_symlink():
            raise HostArtifactExecutionError(
                "host_execution.tree_symlink",
                "authorized host execution trees cannot contain symbolic links",
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise HostArtifactExecutionError(
                "host_execution.tree_entry_invalid",
                "authorized host execution trees require regular files",
            )
        content = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(resolved_root).as_posix(),
                "size": len(content),
                "digest": _digest_bytes(content),
            }
        )
    return canonical_identity(entries).uri


def _capture_entrypoint(
    *,
    artifact_root: Path,
    entrypoint: str,
    artifact_unavailable_code: str,
    entrypoint_unavailable_code: str,
    escape_code: str,
    label: str,
) -> tuple[Path, PurePosixPath, str]:
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise HostArtifactExecutionError(
            artifact_unavailable_code,
            f"{label} must be a regular directory",
        )
    root = artifact_root.resolve(strict=True)
    relative = _relative_entrypoint(entrypoint)
    target = root.joinpath(*relative.parts)
    if target.is_symlink() or not target.is_file():
        raise HostArtifactExecutionError(
            entrypoint_unavailable_code,
            f"{label} entrypoint is unavailable",
        )
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise HostArtifactExecutionError(
            escape_code,
            f"{label} entrypoint escapes its artifact",
        )
    return root, relative, _digest_bytes(resolved.read_bytes())


def _capture_runtime_command(
    command: tuple[str, ...],
) -> tuple[tuple[str, ...], str, str]:
    if (
        not isinstance(command, tuple)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise ValueError("host runtime command must be a non-empty tuple of strings")
    configured = Path(command[0])
    if not configured.is_absolute():
        raise HostArtifactExecutionError(
            "host_execution.runtime_unavailable",
            "host runtime invocation path must be absolute",
        )
    try:
        launcher = configured.resolve(strict=True)
    except OSError as exc:
        raise HostArtifactExecutionError(
            "host_execution.runtime_unavailable",
            "host runtime executable is unavailable",
        ) from exc
    if not launcher.is_file():
        raise HostArtifactExecutionError(
            "host_execution.runtime_unavailable",
            "host runtime executable must be a regular file",
        )
    return (
        command,
        str(launcher),
        _digest_bytes(launcher.read_bytes()),
    )


def _require_node_toolchain_unchanged(
    toolchain: NodeToolchain, *, unavailable_code: str
) -> None:
    """Map exact Node re-probe failures into the host-execution boundary."""

    try:
        toolchain.require_unchanged(_minimal_environment())
    except (BuildError, OSError) as exc:
        raise HostArtifactExecutionError(
            unavailable_code,
            "host Node.js runtime changed or became unavailable",
        ) from exc


@dataclass(frozen=True, slots=True)
class HostAuxiliaryArtifact:
    """Exact auxiliary artifact tree and entrypoint used by a host command."""

    artifact_digest: str
    artifact_root: Path
    artifact_tree_digest: str
    entrypoint: str
    entrypoint_digest: str
    role: str = "backend"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.role, str)
            or not self.role
            or self.role.strip() != self.role
        ):
            raise ValueError(
                "host auxiliary artifact role must be non-empty and trimmed"
            )
        _require_digest(self.artifact_digest, "auxiliary artifact_digest")
        _require_digest(self.artifact_tree_digest, "auxiliary artifact_tree_digest")
        _require_digest(self.entrypoint_digest, "auxiliary entrypoint_digest")
        _relative_entrypoint(self.entrypoint)
        if self.artifact_root.is_symlink() or not self.artifact_root.is_dir():
            raise ValueError("host auxiliary artifact root must be a regular directory")

    @classmethod
    def create(
        cls,
        *,
        artifact_digest: str,
        artifact_root: Path,
        entrypoint: str,
        role: str = "backend",
    ) -> HostAuxiliaryArtifact:
        root, relative, entrypoint_digest = _capture_entrypoint(
            artifact_root=artifact_root,
            entrypoint=entrypoint,
            artifact_unavailable_code=("host_execution.auxiliary_artifact_unavailable"),
            entrypoint_unavailable_code=(
                "host_execution.auxiliary_entrypoint_unavailable"
            ),
            escape_code="host_execution.auxiliary_entrypoint_escape",
            label="host auxiliary artifact",
        )
        return cls(
            artifact_digest=artifact_digest,
            artifact_root=root,
            artifact_tree_digest=_directory_digest(root),
            entrypoint=relative.as_posix(),
            entrypoint_digest=entrypoint_digest,
            role=role,
        )

    @property
    def executable(self) -> Path:
        return self.artifact_root.joinpath(*PurePosixPath(self.entrypoint).parts)

    def require_unchanged(self) -> None:
        """Reject drift from the captured auxiliary tree and entrypoint."""

        executable = self.executable
        try:
            entrypoint_unchanged = (
                not executable.is_symlink()
                and executable.is_file()
                and _digest_bytes(executable.read_bytes()) == self.entrypoint_digest
            )
        except OSError as exc:
            raise HostArtifactExecutionError(
                "host_execution.auxiliary_entrypoint_changed",
                "host auxiliary entrypoint changed after execution authorization",
            ) from exc
        if not entrypoint_unchanged:
            raise HostArtifactExecutionError(
                "host_execution.auxiliary_entrypoint_changed",
                "host auxiliary entrypoint changed after execution authorization",
            )
        if self.artifact_root.is_symlink() or not self.artifact_root.is_dir():
            raise HostArtifactExecutionError(
                "host_execution.auxiliary_input_tree_changed",
                "the authorized host auxiliary artifact tree became unavailable",
            )
        try:
            artifact_tree_digest = _directory_digest(self.artifact_root)
        except (OSError, HostArtifactExecutionError) as exc:
            raise HostArtifactExecutionError(
                "host_execution.auxiliary_input_tree_changed",
                "the authorized host auxiliary artifact tree became unavailable",
            ) from exc
        if artifact_tree_digest != self.artifact_tree_digest:
            raise HostArtifactExecutionError(
                "host_execution.auxiliary_artifact_changed",
                "host auxiliary artifact changed after execution authorization",
            )

    def identity_document(self) -> dict[str, str]:
        return {
            "role": self.role,
            "artifact_digest": self.artifact_digest,
            "artifact_root": str(self.artifact_root),
            "artifact_tree_digest": self.artifact_tree_digest,
            "entrypoint": self.entrypoint,
            "entrypoint_digest": self.entrypoint_digest,
        }


@dataclass(frozen=True, slots=True)
class HostArtifactExecution:
    """Exact operational command material bound into an observation request."""

    language: str
    artifact_digest: str
    artifact_root: Path
    artifact_tree_digest: str
    entrypoint: str
    entrypoint_digest: str
    arguments_json: bytes
    support_paths: tuple[Path, ...]
    support_path_digests: tuple[str, ...]
    runtime_root: Path
    runtime_command: tuple[str, ...] = ()
    runtime_launcher_executable: str | None = None
    runtime_executable_digest: str | None = None
    runtime_toolchain: NodeToolchain | None = None
    auxiliary_artifacts: tuple[HostAuxiliaryArtifact, ...] = ()

    def __post_init__(self) -> None:
        if self.language not in _HOST_EXECUTION_MODES:
            raise ValueError(
                "host artifact language must be python, cpp, rust, swift, "
                "javascript, or rust-javascript-full-stack"
            )
        _require_digest(self.artifact_digest, "artifact_digest")
        _require_digest(self.artifact_tree_digest, "artifact_tree_digest")
        _require_digest(self.entrypoint_digest, "entrypoint_digest")
        _relative_entrypoint(self.entrypoint)
        arguments = json.loads(self.arguments_json)
        if not isinstance(arguments, list) or canonical_json_bytes(arguments) != (
            self.arguments_json
        ):
            raise ValueError("host artifact arguments must be a canonical JSON array")
        if self.artifact_root.is_symlink() or not self.artifact_root.is_dir():
            raise ValueError("host artifact root must be a regular directory")
        if self.runtime_root.is_symlink() or not self.runtime_root.is_dir():
            raise ValueError("host runtime root must be a regular directory")
        if len(self.support_paths) != len(self.support_path_digests):
            raise ValueError("every host support path requires an exact tree digest")
        for path in self.support_paths:
            if path.is_symlink() or not path.is_dir():
                raise ValueError("host support paths must be regular directories")
        for digest in self.support_path_digests:
            _require_digest(digest, "support_path_digest")
        if len(set(self.support_paths)) != len(self.support_paths):
            raise ValueError("host support paths must be unique")
        if self.language in _INTERPRETED_HOST_EXECUTION_MODES:
            if (
                not self.runtime_command
                or any(
                    not isinstance(item, str) or not item
                    for item in self.runtime_command
                )
                or self.runtime_launcher_executable is None
                or self.runtime_executable_digest is None
            ):
                raise ValueError(
                    "interpreted host execution requires an exact runtime command"
                )
            runtime_invocation = Path(self.runtime_command[0])
            runtime_launcher = Path(self.runtime_launcher_executable)
            if (
                not runtime_invocation.is_absolute()
                or not runtime_invocation.is_file()
                or not runtime_launcher.is_absolute()
                or runtime_launcher.is_symlink()
                or not runtime_launcher.is_file()
            ):
                raise ValueError(
                    "host runtime command must bind an exact invocation and launcher"
                )
            try:
                resolved_runtime = runtime_invocation.resolve(strict=True)
            except OSError as exc:
                raise ValueError("host runtime invocation is unavailable") from exc
            if resolved_runtime != runtime_launcher:
                raise ValueError("host runtime invocation resolves to another launcher")
            _require_digest(self.runtime_executable_digest, "runtime_executable_digest")
            if self.language in {"javascript", "rust-javascript-full-stack"}:
                if not isinstance(self.runtime_toolchain, NodeToolchain):
                    raise ValueError(
                        "JavaScript host execution requires an exact Node toolchain"
                    )
                if (
                    self.runtime_command != self.runtime_toolchain.command
                    or self.runtime_launcher_executable
                    != self.runtime_toolchain.launcher_executable
                    or self.runtime_executable_digest
                    != self.runtime_toolchain.launcher_digest
                ):
                    raise ValueError(
                        "host runtime command differs from its exact Node toolchain"
                    )
            elif self.runtime_toolchain is not None:
                raise ValueError(
                    "Python host execution cannot declare a Node toolchain"
                )
        elif (
            self.runtime_command
            or self.runtime_launcher_executable is not None
            or self.runtime_executable_digest is not None
            or self.runtime_toolchain is not None
        ):
            raise ValueError(
                "compiled host execution cannot declare an interpreter command"
            )
        if not isinstance(self.auxiliary_artifacts, tuple) or any(
            not isinstance(artifact, HostAuxiliaryArtifact)
            for artifact in self.auxiliary_artifacts
        ):
            raise ValueError("host auxiliary artifacts must be exact artifact bindings")
        if self.language == "rust-javascript-full-stack":
            if (
                len(self.auxiliary_artifacts) != 1
                or self.auxiliary_artifacts[0].role != "backend"
            ):
                raise ValueError(
                    "full-stack host execution requires one backend auxiliary artifact"
                )
        elif self.auxiliary_artifacts:
            raise ValueError(
                "auxiliary artifacts are only valid for full-stack host execution"
            )

    @classmethod
    def create(
        cls,
        *,
        language: str,
        artifact_digest: str,
        artifact_root: Path,
        entrypoint: str,
        arguments: list[object],
        support_paths: tuple[Path, ...],
        runtime_root: Path,
        runtime_command: tuple[str, ...] | None = None,
        runtime_toolchain: NodeToolchain | None = None,
        auxiliary_artifacts: tuple[HostAuxiliaryArtifact, ...] = (),
    ) -> HostArtifactExecution:
        if runtime_root.is_symlink() or not runtime_root.is_dir():
            raise HostArtifactExecutionError(
                "host_execution.runtime_unavailable",
                "host runtime must be a regular directory",
            )
        if any(path.is_symlink() or not path.is_dir() for path in support_paths):
            raise HostArtifactExecutionError(
                "host_execution.support_path_unavailable",
                "host support paths must be regular directories",
            )
        root, relative, entrypoint_digest = _capture_entrypoint(
            artifact_root=artifact_root,
            entrypoint=entrypoint,
            artifact_unavailable_code="host_execution.artifact_unavailable",
            entrypoint_unavailable_code="host_execution.entrypoint_unavailable",
            escape_code="host_execution.entrypoint_escape",
            label="compiled host artifact",
        )
        if language == "python" and runtime_command is None:
            runtime_command = (sys.executable,)
        if language in {"javascript", "rust-javascript-full-stack"}:
            if not isinstance(runtime_toolchain, NodeToolchain):
                raise HostArtifactExecutionError(
                    "host_execution.runtime_unavailable",
                    "JavaScript host execution requires an exact Node toolchain",
                )
            if runtime_command is None:
                runtime_command = runtime_toolchain.command
            elif runtime_command != runtime_toolchain.command:
                raise HostArtifactExecutionError(
                    "host_execution.runtime_mismatch",
                    "host runtime command differs from its exact Node toolchain",
                )
            _require_node_toolchain_unchanged(
                runtime_toolchain,
                unavailable_code="host_execution.runtime_unavailable",
            )
        elif runtime_toolchain is not None:
            raise ValueError(
                "non-JavaScript host execution cannot declare a Node toolchain"
            )
        if language in _INTERPRETED_HOST_EXECUTION_MODES:
            if runtime_command is None:
                raise HostArtifactExecutionError(
                    "host_execution.runtime_unavailable",
                    "interpreted host execution requires a runtime command",
                )
            (
                exact_runtime_command,
                runtime_launcher_executable,
                runtime_executable_digest,
            ) = _capture_runtime_command(runtime_command)
            if runtime_toolchain is not None:
                _require_node_toolchain_unchanged(
                    runtime_toolchain,
                    unavailable_code="host_execution.runtime_unavailable",
                )
        else:
            if runtime_command is not None:
                raise ValueError(
                    "compiled host execution cannot declare an interpreter command"
                )
            exact_runtime_command = ()
            runtime_launcher_executable = None
            runtime_executable_digest = None
        resolved_support = tuple(path.resolve(strict=True) for path in support_paths)
        return cls(
            language=language,
            artifact_digest=artifact_digest,
            artifact_root=root,
            artifact_tree_digest=_directory_digest(root),
            entrypoint=relative.as_posix(),
            entrypoint_digest=entrypoint_digest,
            arguments_json=canonical_json_bytes(arguments),
            support_paths=resolved_support,
            support_path_digests=tuple(
                _directory_digest(path) for path in resolved_support
            ),
            runtime_root=runtime_root.resolve(strict=True),
            runtime_command=exact_runtime_command,
            runtime_launcher_executable=runtime_launcher_executable,
            runtime_executable_digest=runtime_executable_digest,
            runtime_toolchain=runtime_toolchain,
            auxiliary_artifacts=auxiliary_artifacts,
        )

    @property
    def executable(self) -> Path:
        return self.artifact_root.joinpath(*PurePosixPath(self.entrypoint).parts)

    def with_arguments(self, arguments: list[object]) -> HostArtifactExecution:
        """Derive a command while retaining this descriptor's exact input identities."""

        derived = replace(self, arguments_json=canonical_json_bytes(arguments))
        derived.require_inputs_unchanged()
        return derived

    def require_inputs_unchanged(self) -> None:
        """Reject drift from every executable and input tree captured at creation."""

        executable = self.executable
        try:
            entrypoint_unchanged = (
                not executable.is_symlink()
                and executable.is_file()
                and _digest_bytes(executable.read_bytes()) == self.entrypoint_digest
            )
        except OSError as exc:
            raise HostArtifactExecutionError(
                "host_execution.entrypoint_changed",
                "compiled entrypoint changed after execution authorization",
            ) from exc
        if not entrypoint_unchanged:
            raise HostArtifactExecutionError(
                "host_execution.entrypoint_changed",
                "compiled entrypoint changed after execution authorization",
            )

        if self.artifact_root.is_symlink() or not self.artifact_root.is_dir():
            raise HostArtifactExecutionError(
                "host_execution.input_tree_changed",
                "the authorized host artifact tree became unavailable",
            )
        if any(path.is_symlink() or not path.is_dir() for path in self.support_paths):
            raise HostArtifactExecutionError(
                "host_execution.input_tree_changed",
                "an authorized host support tree became unavailable",
            )
        try:
            artifact_tree_digest = _directory_digest(self.artifact_root)
            support_path_digests = tuple(
                _directory_digest(path) for path in self.support_paths
            )
        except (OSError, HostArtifactExecutionError) as exc:
            raise HostArtifactExecutionError(
                "host_execution.input_tree_changed",
                "an authorized host execution tree became unavailable",
            ) from exc
        if artifact_tree_digest != self.artifact_tree_digest:
            raise HostArtifactExecutionError(
                "host_execution.artifact_changed",
                "compiled artifact changed after execution authorization",
            )
        if support_path_digests != self.support_path_digests:
            raise HostArtifactExecutionError(
                "host_execution.support_path_changed",
                "host support code changed after execution authorization",
            )
        if self.runtime_command:
            runtime_invocation = Path(self.runtime_command[0])
            try:
                runtime_launcher = runtime_invocation.resolve(strict=True)
                runtime_unchanged = (
                    str(runtime_launcher) == self.runtime_launcher_executable
                    and runtime_launcher.is_file()
                    and _digest_bytes(runtime_launcher.read_bytes())
                    == self.runtime_executable_digest
                )
            except OSError as exc:
                raise HostArtifactExecutionError(
                    "host_execution.runtime_changed",
                    "host runtime executable changed after execution authorization",
                ) from exc
            if not runtime_unchanged:
                raise HostArtifactExecutionError(
                    "host_execution.runtime_changed",
                    "host runtime executable changed after execution authorization",
                )
        if self.runtime_toolchain is not None:
            _require_node_toolchain_unchanged(
                self.runtime_toolchain,
                unavailable_code="host_execution.runtime_changed",
            )
        for auxiliary in self.auxiliary_artifacts:
            auxiliary.require_unchanged()

    @property
    def harness_digest(self) -> str:
        return canonical_identity(
            {
                "runner_id": AUTHORIZED_HOST_RUNNER_ID,
                "language": self.language,
                "artifact_digest": self.artifact_digest,
                "artifact_root": str(self.artifact_root),
                "artifact_tree_digest": self.artifact_tree_digest,
                "entrypoint": self.entrypoint,
                "entrypoint_digest": self.entrypoint_digest,
                "arguments": json.loads(self.arguments_json),
                "runtime_root": str(self.runtime_root),
                "runtime_command": list(self.runtime_command),
                "runtime_launcher_executable": self.runtime_launcher_executable,
                "runtime_executable_digest": self.runtime_executable_digest,
                "runtime_toolchain": (
                    {
                        "identity": self.runtime_toolchain.identity,
                        "command": list(self.runtime_toolchain.command),
                        "launcher_executable": (
                            self.runtime_toolchain.launcher_executable
                        ),
                        "launcher_digest": self.runtime_toolchain.launcher_digest,
                        "runtime_executable": (
                            self.runtime_toolchain.runtime_executable
                        ),
                        "runtime_digest": self.runtime_toolchain.runtime_digest,
                        "version": self.runtime_toolchain.version,
                    }
                    if self.runtime_toolchain is not None
                    else None
                ),
                "python_executable": (
                    self.runtime_command[0] if self.language == "python" else None
                ),
                "auxiliary_artifacts": [
                    artifact.identity_document()
                    for artifact in self.auxiliary_artifacts
                ],
                "support_paths": [
                    {"path": str(path), "tree_digest": digest}
                    for path, digest in zip(
                        self.support_paths, self.support_path_digests, strict=True
                    )
                ],
            }
        ).uri

    def observation_request(
        self,
        *,
        effective_revision_digest: str,
        source_digests: tuple[str, ...],
    ) -> ObservationRequest:
        return ObservationRequest(
            effective_revision_digest=effective_revision_digest,
            source_digests=source_digests,
            runner_id=AUTHORIZED_HOST_RUNNER_ID,
            harness_digest=self.harness_digest,
            sandbox_profile=AUTHORIZED_HOST_PROCESS_PROFILE,
            requested_privileges=AUTHORIZED_HOST_PRIVILEGES,
            allowed_outputs=("stdout", "stderr"),
        )


@dataclass(frozen=True, slots=True)
class HostAuxiliaryExecutionResult:
    """Observed result from one exact auxiliary role in a composite execution."""

    role: str
    result: object
    stdout_digest: str
    stderr_digest: str


@dataclass(frozen=True, slots=True)
class HostArtifactExecutionResult:
    result: object
    execution_mode: str
    authorization_id: str
    stdout_digest: str
    stderr_digest: str
    auxiliary_results: tuple[HostAuxiliaryExecutionResult, ...] = ()


class AuthorizedHostArtifactRunner:
    """Run only an exact artifact carrying an exact, live observation grant.

    This portable backend is deliberately named a host process, not a sandbox. Callers
    must explicitly acknowledge that generated code receives host-process authority.
    """

    runner_id = AUTHORIZED_HOST_RUNNER_ID

    def __init__(
        self,
        *,
        host_execution_acknowledged: bool = False,
        timeout_seconds: float = 60.0,
        stdout_limit_bytes: int = DEFAULT_HOST_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_HOST_STDERR_LIMIT_BYTES,
        authorization_verifier: ObservationExecutionAuthorizationVerifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("host execution timeout must be positive")
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("host execution output limits must be positive")
        self.host_execution_acknowledged = host_execution_acknowledged
        self.timeout_seconds = timeout_seconds
        self.stdout_limit_bytes = stdout_limit_bytes
        self.stderr_limit_bytes = stderr_limit_bytes
        self.authorization_verifier = (
            authorization_verifier
            or FailClosedObservationExecutionAuthorizationVerifier()
        )
        self.clock = clock

    def run(
        self,
        execution: HostArtifactExecution,
        request: ObservationRequest,
        authorization: ObservationExecutionAuthorization,
    ) -> HostArtifactExecutionResult:
        expected = execution.observation_request(
            effective_revision_digest=request.effective_revision_digest,
            source_digests=request.source_digests,
        )
        if request != expected:
            raise HostArtifactExecutionError(
                "host_execution.request_mismatch",
                "host execution request does not match the exact artifact command",
            )
        if authorization.privileges != tuple(sorted(request.requested_privileges)):
            raise HostArtifactExecutionError(
                "host_execution.authorization_privilege_mismatch",
                "host execution grant does not carry every requested privilege",
            )
        if not self.host_execution_acknowledged:
            raise HostArtifactExecutionError(
                "host_execution.acknowledgement_required",
                "generated host-process execution requires explicit acknowledgement",
            )
        executable = execution.executable
        environment = _minimal_environment()
        execution.require_inputs_unchanged()
        self.authorization_verifier.require_observation_valid(
            authorization,
            request,
            now=self.clock(),
        )
        # Recheck after the live authorization lookup so the input check is the last
        # userspace operation before process creation. Filesystem checks and launch are
        # still not atomic; production isolation must provide immutable inputs.
        execution.require_inputs_unchanged()
        auxiliary_results: tuple[HostAuxiliaryExecutionResult, ...] = ()
        if execution.language == "rust-javascript-full-stack":
            backend_command = self._command(execution)
            backend_completed = self._run_command(
                execution,
                backend_command,
                environment,
                role="backend",
            )
            backend_result = self._json_output(
                backend_completed,
                role="backend",
                command=backend_command,
                environment=environment,
            )
            auxiliary_results = (
                HostAuxiliaryExecutionResult(
                    role="backend",
                    result=backend_result,
                    stdout_digest=_digest_bytes(backend_completed.stdout),
                    stderr_digest=_digest_bytes(backend_completed.stderr),
                ),
            )
            frontend_command = self._frontend_command(execution, backend_result)
            completed = self._run_command(
                execution,
                frontend_command,
                environment,
                role="frontend",
            )
            output_command = frontend_command
        else:
            application_command = self._command(execution)
            completed = self._run_command(
                execution,
                application_command,
                environment,
                role="application",
            )
            output_command = application_command
        # Never accept even successful output from a process that changed its own
        # authorized artifact or support inputs while it ran.
        execution.require_inputs_unchanged()
        value = self._json_output(
            completed,
            role=(
                "frontend"
                if execution.language == "rust-javascript-full-stack"
                else "application"
            ),
            command=output_command,
            environment=environment,
        )
        if execution.language == "python":
            if not isinstance(value, dict) or value.get("module_file") != str(
                executable.resolve(strict=True)
            ):
                raise HostArtifactExecutionError(
                    "host_execution.artifact_mismatch",
                    "Python host process loaded a different bytecode artifact",
                )
            result = value.get("result")
            mode = "authorized-host-python-bytecode"
        else:
            result = value
            mode = {
                "cpp": "authorized-host-cpp-executable",
                "rust": "authorized-host-rust-executable",
                "swift": "authorized-host-swift-executable",
                "javascript": "authorized-host-javascript-node",
                "rust-javascript-full-stack": (
                    "authorized-host-rust-javascript-full-stack"
                ),
            }[execution.language]
        return HostArtifactExecutionResult(
            result=result,
            execution_mode=mode,
            authorization_id=authorization.authorization_id,
            stdout_digest=_digest_bytes(completed.stdout),
            stderr_digest=_digest_bytes(completed.stderr),
            auxiliary_results=auxiliary_results,
        )

    def _run_command(
        self,
        execution: HostArtifactExecution,
        command: list[str],
        environment: dict[str, str],
        *,
        role: str,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = _run_bounded_process(
                command,
                cwd=execution.runtime_root,
                env=environment,
                timeout_seconds=self.timeout_seconds,
                stdout_limit_bytes=self.stdout_limit_bytes,
                stderr_limit_bytes=self.stderr_limit_bytes,
            )
        except HostArtifactExecutionError as exc:
            _retain_host_execution_failure(
                role=role,
                code=exc.code,
                returncode=exc.returncode,
                command=command,
                stdout=exc.stdout,
                stderr=exc.stderr,
                environment=environment,
            )
            raise
        execution.require_inputs_unchanged()
        if completed.returncode != 0:
            _retain_host_execution_failure(
                role=role,
                code="host_execution.nonzero_exit",
                returncode=completed.returncode,
                command=command,
                stdout=completed.stdout,
                stderr=completed.stderr,
                environment=environment,
            )
            detail = (
                completed.stderr[-2000:].decode("utf-8", errors="replace").strip()
                or completed.stdout[-2000:].decode("utf-8", errors="replace").strip()
            )
            suffix = f": {detail}" if detail else ""
            raise HostArtifactExecutionError(
                "host_execution.nonzero_exit",
                f"authorized {role} host process returned status "
                f"{completed.returncode}{suffix}",
                role=role,
                returncode=completed.returncode,
                stdout_digest=_digest_bytes(completed.stdout),
                stderr_digest=_digest_bytes(completed.stderr),
            )
        return completed

    @staticmethod
    def _json_output(
        completed: subprocess.CompletedProcess[bytes],
        *,
        role: str,
        command: list[str],
        environment: Mapping[str, str],
    ) -> object:
        try:
            return json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _retain_host_execution_failure(
                role=role,
                code="host_execution.output_invalid",
                returncode=completed.returncode,
                command=command,
                stdout=completed.stdout,
                stderr=completed.stderr,
                environment=environment,
            )
            raise HostArtifactExecutionError(
                "host_execution.output_invalid",
                f"authorized {role} host process did not emit one JSON value",
            ) from exc

    @staticmethod
    def _command(execution: HostArtifactExecution) -> list[str]:
        arguments = execution.arguments_json.decode("utf-8")
        if os.name == "nt" and execution.language == "cpp":
            # Windows creates the process from a Unicode command line, but a portable
            # C++ `main(int, char**)` receives bytes through the active narrow code
            # page. JSON's ASCII escape form preserves the exact authorized value
            # without requiring generated applications to depend on that host locale.
            arguments = json.dumps(
                json.loads(execution.arguments_json),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        if execution.language == "python":
            return [
                *execution.runtime_command,
                "-I",
                "-S",
                "-B",
                "-P",
                "-X",
                f"pycache_prefix={execution.runtime_root / 'pycache'}",
                "-c",
                _PYTHON_HOST_RUNNER,
                str(execution.executable),
                arguments,
                canonical_json_bytes(
                    [str(path) for path in execution.support_paths]
                ).decode("utf-8"),
            ]
        if execution.language == "javascript":
            return [
                *execution.runtime_command,
                str(execution.executable),
                arguments,
            ]
        if execution.language == "rust-javascript-full-stack":
            backend = execution.auxiliary_artifacts[0]
            return [str(backend.executable), arguments]
        return [str(execution.executable), arguments]

    @staticmethod
    def _frontend_command(
        execution: HostArtifactExecution, backend_result: object
    ) -> list[str]:
        if execution.language != "rust-javascript-full-stack":
            raise ValueError("frontend commands require a full-stack execution")
        return [
            *execution.runtime_command,
            str(execution.executable),
            canonical_json_bytes(backend_result).decode("utf-8"),
        ]


def _minimal_environment() -> dict[str, str]:
    environment = {"PATH": os.defpath}
    for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    if os.name != "nt":
        environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    return environment


def _retain_host_execution_failure(
    *,
    role: str,
    code: str,
    returncode: int | None,
    command: list[str],
    stdout: bytes | None,
    stderr: bytes | None,
    environment: Mapping[str, str],
) -> None:
    """Retain one redacted authorized-host failure transcript in the ambient run."""

    run = attach_run()
    if run is None:
        return
    try:
        context = run.node(
            f"host-execution/{role}/failure",
            operation=code,
            pins={
                "role": role,
                "error_code": code,
                "returncode": returncode,
                "argv": list(command),
            },
        )
        with context as node:
            if isinstance(node, EvidenceNode):
                node.attach_text(
                    "stdout.log",
                    redact_secrets(
                        (stdout or b"").decode("utf-8", errors="replace"),
                        environment,
                    ),
                    role="stdout",
                )
                node.attach_text(
                    "stderr.log",
                    redact_secrets(
                        (stderr or b"").decode("utf-8", errors="replace"),
                        environment,
                    ),
                    role="stderr",
                )
                node.fail(
                    f"{code}: authorized {role} host process returned status "
                    f"{returncode}"
                )
    except Exception:
        return


def _run_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> subprocess.CompletedProcess[bytes]:
    """Drain both pipes concurrently, killing before either exceeds its budget."""

    child_environment = inherited_verbose_environment(env)
    ownership = create_process_tree_ownership()
    try:
        _started = datetime.now(UTC)
        trace_subprocess(command, cwd=cwd, environment=child_environment)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **ownership.popen_options,
        )
    except OSError as exc:
        ownership.release()
        raise HostArtifactExecutionError(
            "host_execution.failed", "authorized host process could not start"
        ) from exc
    ownership.bind(process.pid)
    assert process.stdout is not None and process.stderr is not None
    overflow = threading.Event()
    read_errors: list[OSError | ValueError] = []
    state_lock = threading.Lock()
    stdout = bytearray()
    stderr = bytearray()

    def drain(stream, target: bytearray, limit: int) -> None:
        try:
            while chunk := stream.read(_PROCESS_READ_CHUNK_BYTES):
                remaining = limit - len(target)
                if len(chunk) > remaining:
                    if remaining > 0:
                        target.extend(chunk[:remaining])
                    overflow.set()
                    _terminate_process_tree(process, ownership=ownership)
                    return
                target.extend(chunk)
        except (OSError, ValueError) as exc:
            with state_lock:
                read_errors.append(exc)
            _terminate_process_tree(process, ownership=ownership)

    readers = (
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout, stdout_limit_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr, stderr_limit_bytes),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process, ownership=ownership)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        _close_process_pipes(process)
        for reader in readers:
            reader.join(timeout=1)
        ownership.release()
        raise HostArtifactExecutionError(
            "host_execution.timeout",
            "authorized host process exceeded its deadline",
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        ) from exc
    pipe_close_deadline = time.monotonic() + _PIPE_CLOSE_TIMEOUT_SECONDS
    for reader in readers:
        reader.join(timeout=max(0.0, pipe_close_deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        _terminate_process_tree(process, ownership=ownership)
        descendant_close_deadline = time.monotonic() + _PIPE_CLOSE_TIMEOUT_SECONDS
        for reader in readers:
            reader.join(timeout=max(0.0, descendant_close_deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers):
            _close_process_pipes(process)
            for reader in readers:
                reader.join(timeout=1)
            ownership.release()
            raise HostArtifactExecutionError(
                "host_execution.failed",
                "authorized host process left inherited output streams open",
                stdout=bytes(stdout),
                stderr=bytes(stderr),
            )
    _close_process_pipes(process)
    if read_errors:
        ownership.release()
        raise HostArtifactExecutionError(
            "host_execution.failed",
            "authorized host process output could not be read",
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        ) from read_errors[0]
    if overflow.is_set():
        ownership.release()
        raise HostArtifactExecutionError(
            "host_execution.output_limit",
            "authorized host process exceeded its stdout or stderr byte budget",
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        )
    completed = subprocess.CompletedProcess(
        command, returncode, bytes(stdout), bytes(stderr)
    )
    trace_subprocess(
        command,
        cwd=cwd,
        environment=child_environment,
        status=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        started_at=_started,
    )
    ownership.release()
    return completed


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    if process.stdout is not None:
        with suppress(OSError):
            process.stdout.close()
    if process.stderr is not None:
        with suppress(OSError):
            process.stderr.close()


__all__ = [
    "AUTHORIZED_HOST_PROCESS_PROFILE",
    "AUTHORIZED_HOST_PRIVILEGES",
    "AUTHORIZED_HOST_RUNNER_ID",
    "DEFAULT_HOST_STDERR_LIMIT_BYTES",
    "DEFAULT_HOST_STDOUT_LIMIT_BYTES",
    "AuthorizedHostArtifactRunner",
    "HostAuxiliaryArtifact",
    "HostArtifactExecution",
    "HostArtifactExecutionError",
    "HostArtifactExecutionResult",
]
