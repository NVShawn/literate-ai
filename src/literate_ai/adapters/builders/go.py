"""Authorization-gated portable Go builder and host toolchain discovery."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from literate_ai.ports import BuildInputConsumption
from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
)

from ._process import run_bounded_process as _run_bounded_process
from .python import (
    BuildError,
    _require_source_tree_unchanged,
    canonical_tree_digest,
    executable_file_digest,
    require_unsandboxed_host_build_authorization,
)

DEFAULT_GO_VERSION_TIMEOUT_SECONDS = 15.0
DEFAULT_GO_BUILD_TIMEOUT_SECONDS = 120.0
DEFAULT_GO_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_GO_STDERR_LIMIT_BYTES = 1024 * 1024


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a sha256 digest")
    encoded = value.removeprefix("sha256:")
    if (
        not value.startswith("sha256:")
        or len(encoded) != 64
        or encoded != encoded.casefold()
    ):
        raise ValueError(f"{label} must be a sha256 digest")
    try:
        int(encoded, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a sha256 digest") from exc


def _go_toolchain_identity(
    command: tuple[str, ...],
    executable_path: str,
    executable_digest: str,
    version: str,
) -> str:
    encoded = json.dumps(
        {
            "command": list(command),
            "executable_digest": executable_digest,
            "executable_path": executable_path,
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class GoToolchain:
    """One ``go`` command route with an exact bound compiler executable."""

    command: tuple[str, ...]
    executable_path: str
    executable_digest: str
    version: str
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
            or not isinstance(self.executable_path, str)
            or not self.executable_path
            or not isinstance(self.version, str)
            or not self.version
        ):
            raise ValueError("Go toolchain must identify a versioned go command")
        _require_sha256(self.executable_digest, "Go executable identity")
        object.__setattr__(
            self,
            "identity",
            _go_toolchain_identity(
                self.command,
                self.executable_path,
                self.executable_digest,
                self.version,
            ),
        )

    def _require_files_unchanged(self) -> None:
        configured = Path(self.command[0])
        try:
            resolved = configured.resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.go_toolchain_changed",
                "Go compiler became unavailable after toolchain selection",
            ) from exc
        if str(resolved) != self.executable_path or not resolved.is_file():
            raise BuildError(
                "builder.go_toolchain_changed",
                "Go compiler path changed after toolchain selection",
            )
        if executable_file_digest(resolved) != self.executable_digest:
            raise BuildError(
                "builder.go_toolchain_changed",
                "Go compiler bytes changed after toolchain selection",
            )

    def require_unchanged(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        timeout_seconds: float = DEFAULT_GO_VERSION_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_GO_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_GO_STDERR_LIMIT_BYTES,
    ) -> None:
        configured = dict(os.environ if environment is None else environment)
        self._require_files_unchanged()
        version = _probe_go_command(
            self.command,
            configured,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            unavailable_code="builder.go_toolchain_changed",
        )
        self._require_files_unchanged()
        if version != self.version:
            raise BuildError(
                "builder.go_toolchain_changed",
                "Go version changed after toolchain selection",
            )


def _probe_go_command(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    unavailable_code: str,
) -> str:
    result = _run_bounded_process(
        [*command, "version"],
        cwd=None,
        environment=environment,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        error_prefix="builder.go_version",
    )
    version = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not version:
        raise BuildError(unavailable_code, "go did not report a usable version")
    return version


def discover_go_toolchain(
    environment: Mapping[str, str] | None = None,
    *,
    timeout_seconds: float = DEFAULT_GO_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_GO_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_GO_STDERR_LIMIT_BYTES,
) -> GoToolchain:
    """Resolve ``GOTOOLCHAIN_CMD`` or ``go`` from the caller's explicit ``PATH``."""

    configured = dict(os.environ if environment is None else environment)
    raw = configured.get("GOTOOLCHAIN_CMD", "").strip()
    if raw:
        try:
            parsed = tuple(shlex.split(raw, posix=os.name != "nt"))
        except ValueError as exc:
            raise BuildError(
                "builder.go_toolchain", "GOTOOLCHAIN_CMD is not a valid command"
            ) from exc
        if os.name == "nt" and parsed:
            executable = parsed[0]
            if (
                len(executable) >= 2
                and executable[0] == executable[-1]
                and executable[0] in {'"', "'"}
            ):
                parsed = (executable[1:-1], *parsed[1:])
        if not parsed:
            raise BuildError("builder.go_toolchain", "GOTOOLCHAIN_CMD is empty")
    else:
        parsed = ("go",)

    executable = shutil.which(parsed[0], path=configured.get("PATH"))
    if executable is None:
        raise BuildError(
            "builder.go_toolchain_unavailable",
            "go was not found through GOTOOLCHAIN_CMD or PATH",
        )
    invocation_path = Path(os.path.abspath(executable))
    try:
        resolved_executable = invocation_path.resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.go_toolchain_unavailable",
            "go became unavailable during toolchain discovery",
        ) from exc
    if not resolved_executable.is_file():
        raise BuildError(
            "builder.go_toolchain_unavailable",
            "go does not resolve to a regular executable file",
        )
    command = (str(invocation_path), *parsed[1:])
    executable_digest = executable_file_digest(resolved_executable)
    try:
        version = _probe_go_command(
            command,
            configured,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            unavailable_code="builder.go_toolchain_unavailable",
        )
    finally:
        try:
            current_executable = invocation_path.resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.go_toolchain_changed",
                "Go compiler became unavailable during toolchain discovery",
            ) from exc
        if (
            current_executable != resolved_executable
            or executable_file_digest(current_executable) != executable_digest
        ):
            raise BuildError(
                "builder.go_toolchain_changed",
                "Go compiler bytes changed during toolchain discovery",
            )
    return GoToolchain(
        command,
        str(resolved_executable),
        executable_digest,
        version,
    )


@dataclass(frozen=True, slots=True)
class GoBuildArtifact:
    artifact_digest: str
    artifact_path: Path
    source_bundle_digest: str
    authorization_id: str
    executable_file: str
    compiler_identity: str
    consumed_source_files: tuple[str, ...]
    lifecycle_consumed_files: tuple[str, ...]
    build_system_consumed_files: tuple[str, ...]
    build_input_consumption_identity: str | None


def _require_generated_files_covered(
    source_root: Path, *covered_file_groups: tuple[str, ...]
) -> None:
    """Reject every generated file that no selected toolchain operation consumes."""

    actual: set[str] = set()
    for path in source_root.rglob("*"):
        if path.is_symlink():
            raise BuildError(
                "builder.source_symlink", f"Source symlink rejected: {path}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise BuildError(
                "builder.source_node_invalid",
                f"Generated source contains a non-file node: {path}",
            )
        actual.add(path.relative_to(source_root).as_posix())

    covered: set[str] = set()
    for group in covered_file_groups:
        group_set = set(group)
        if len(group_set) != len(group):
            raise BuildError(
                "builder.generated_input_coverage_invalid",
                "Each build input consumer must name generated files exactly once",
            )
        covered.update(group_set)
    unknown = covered.difference(actual)
    if unknown:
        raise BuildError(
            "builder.generated_input_coverage_invalid",
            "Build input coverage named files outside the generated tree: "
            + ", ".join(sorted(unknown)[:5]),
        )
    uncovered = actual.difference(covered)
    if uncovered:
        displayed = ", ".join(sorted(uncovered)[:5])
        suffix = "" if len(uncovered) <= 5 else ", ..."
        raise BuildError(
            "builder.generated_file_unconsumed",
            "Generated files were neither consumed by the compiler nor checked "
            f"and published: {displayed}{suffix}",
        )


class GuardedGoBuilder:
    """Compile a generated single-file Go binary after build authorization checks.

    The selected language skill confines generated Go source to one standard-library
    file at ``source/main.go`` with no external module dependency, mirroring the Rust
    Flavor's single crate-root constraint. That closed input surface makes per-file
    dependency-manifest parsing unnecessary: the sole consumed source file is always
    the entrypoint itself.
    """

    builder_id = "builder:go-native@1"

    def __init__(
        self,
        toolchain: GoToolchain,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        timeout_seconds: float = DEFAULT_GO_BUILD_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_GO_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_GO_STDERR_LIMIT_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Go build timeout must be positive")
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("Go compiler output limits must be positive")
        self.toolchain = toolchain
        self.authorization_verifier = (
            authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self.timeout_seconds = timeout_seconds
        self.stdout_limit_bytes = stdout_limit_bytes
        self.stderr_limit_bytes = stderr_limit_bytes

    def build(
        self,
        request: BuildRequest,
        authorization: BuildAuthorization,
        *,
        source_root: Path,
        artifact_store: Path,
        now: datetime,
        lifecycle_consumed_files: tuple[str, ...] = (),
        build_input_consumption: BuildInputConsumption | None = None,
    ) -> GoBuildArtifact:
        self.authorization_verifier.require_build_valid(authorization, request, now=now)
        require_unsandboxed_host_build_authorization(request, authorization)
        if request.builder_id != self.builder_id:
            raise BuildError(
                "builder.identity_mismatch", "Build request selected another builder"
            )
        if "native-executable" not in request.allowed_outputs:
            raise BuildError(
                "builder.output_not_authorized",
                "Native executable output is not authorized",
            )
        if request.toolchain_digest != self.toolchain.identity:
            raise BuildError(
                "builder.toolchain_mismatch",
                "Build request selected another Go toolchain",
            )

        self.toolchain.require_unchanged()
        actual_source_digest = canonical_tree_digest(source_root)
        if actual_source_digest != request.source_bundle_digest:
            raise BuildError(
                "builder.source_digest_mismatch",
                "Source tree changed after authorization",
            )
        if (
            build_input_consumption is not None
            and build_input_consumption.source_bundle_digest != actual_source_digest
        ):
            raise BuildError(
                "builder.build_input_consumption_mismatch",
                "Build-system input evidence selected another generated tree",
            )
        build_system_consumed_files = (
            build_input_consumption.files if build_input_consumption is not None else ()
        )
        resolved_source = source_root.resolve(strict=True)
        entrypoint = resolved_source / "source" / "main.go"
        if entrypoint.is_symlink() or not entrypoint.is_file():
            raise BuildError(
                "builder.go_entrypoint_missing",
                "Generated Go source must provide source/main.go",
            )

        destination_root = artifact_store.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="go-build-", dir=destination_root))
        compiler_output = Path(tempfile.mkdtemp(prefix="literate-go-"))
        executable_name = "sample.exe" if os.name == "nt" else "sample"
        compiled_executable = compiler_output / executable_name
        executable = staging / executable_name
        command = [
            *self.toolchain.command,
            "build",
            "-o",
            str(compiled_executable),
            "source/main.go",
        ]
        build_environment = dict(os.environ)
        build_environment["GO111MODULE"] = "off"
        build_environment["GOFLAGS"] = "-mod=mod"
        build_environment.setdefault("GOCACHE", str(compiler_output / "go-build-cache"))
        try:
            try:
                completed = _run_bounded_process(
                    command,
                    cwd=resolved_source,
                    environment=build_environment,
                    timeout_seconds=self.timeout_seconds,
                    stdout_limit_bytes=self.stdout_limit_bytes,
                    stderr_limit_bytes=self.stderr_limit_bytes,
                    error_prefix="builder.go_compile",
                )
            finally:
                self.toolchain.require_unchanged()
                _require_source_tree_unchanged(resolved_source, actual_source_digest)
            if completed.returncode != 0 or not compiled_executable.is_file():
                detail = (completed.stdout + completed.stderr)[-4000:].decode(
                    "utf-8", errors="replace"
                )
                raise BuildError(
                    "builder.go_compile_failed", "Go compilation failed: " + detail
                )

            consumed_source_files = ("source/main.go",)
            shutil.copy2(compiled_executable, executable)
            _require_generated_files_covered(
                resolved_source,
                consumed_source_files,
                lifecycle_consumed_files,
                build_system_consumed_files,
            )
            _require_source_tree_unchanged(resolved_source, actual_source_digest)

            executable_digest = (
                f"sha256:{hashlib.sha256(executable.read_bytes()).hexdigest()}"
            )
            manifest = {
                "authorization_id": authorization.authorization_id,
                "builder_id": self.builder_id,
                "compiler_identity": self.toolchain.identity,
                "consumed_source_files": list(consumed_source_files),
                "lifecycle_consumed_files": list(lifecycle_consumed_files),
                "build_system_consumed_files": list(build_system_consumed_files),
                "build_input_consumption_identity": (
                    build_input_consumption.identity
                    if build_input_consumption is not None
                    else None
                ),
                "executable_digest": executable_digest,
                "executable_file": executable_name,
                "source_bundle_digest": actual_source_digest,
            }
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            artifact_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            (staging / "build-manifest.json").write_bytes(manifest_bytes)
            if not _cached_go_artifact_matches(
                staging,
                executable_name=executable_name,
                executable_digest=executable_digest,
                manifest_bytes=manifest_bytes,
            ):
                raise BuildError(
                    "builder.artifact_invalid",
                    "Built artifact differs from its exact manifest",
                )
            final = destination_root / artifact_digest.removeprefix("sha256:")
            if final.exists():
                if not _cached_go_artifact_matches(
                    final,
                    executable_name=executable_name,
                    executable_digest=executable_digest,
                    manifest_bytes=manifest_bytes,
                ):
                    raise BuildError(
                        "builder.artifact_collision", "Existing artifact differs"
                    )
                shutil.rmtree(staging)
            else:
                os.replace(staging, final)
            return GoBuildArtifact(
                artifact_digest=artifact_digest,
                artifact_path=final,
                source_bundle_digest=actual_source_digest,
                authorization_id=authorization.authorization_id,
                executable_file=executable_name,
                compiler_identity=self.toolchain.identity,
                consumed_source_files=consumed_source_files,
                lifecycle_consumed_files=lifecycle_consumed_files,
                build_system_consumed_files=build_system_consumed_files,
                build_input_consumption_identity=(
                    build_input_consumption.identity
                    if build_input_consumption is not None
                    else None
                ),
            )
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        finally:
            shutil.rmtree(compiler_output, ignore_errors=True)


def _cached_go_artifact_matches(
    root: Path,
    *,
    executable_name: str,
    executable_digest: str,
    manifest_bytes: bytes,
) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            return False
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            return False
    if actual_files != {"build-manifest.json", executable_name}:
        return False
    manifest = root / "build-manifest.json"
    executable = root / executable_name
    return (
        manifest.read_bytes() == manifest_bytes
        and f"sha256:{hashlib.sha256(executable.read_bytes()).hexdigest()}"
        == executable_digest
    )


__all__ = [
    "DEFAULT_GO_BUILD_TIMEOUT_SECONDS",
    "DEFAULT_GO_STDERR_LIMIT_BYTES",
    "DEFAULT_GO_STDOUT_LIMIT_BYTES",
    "DEFAULT_GO_VERSION_TIMEOUT_SECONDS",
    "GoBuildArtifact",
    "GoToolchain",
    "GuardedGoBuilder",
    "discover_go_toolchain",
]
