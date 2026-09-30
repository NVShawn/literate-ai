"""Authorization-gated portable Rust builder and host toolchain discovery."""

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

DEFAULT_RUST_VERSION_TIMEOUT_SECONDS = 15.0
DEFAULT_RUST_BUILD_TIMEOUT_SECONDS = 120.0
DEFAULT_RUSTC_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_RUSTC_STDERR_LIMIT_BYTES = 1024 * 1024


def _rust_toolchain_identity(
    command: tuple[str, ...],
    launcher_executable: str,
    launcher_digest: str,
    compiler_executable: str,
    compiler_digest: str,
    version: str,
) -> str:
    encoded = json.dumps(
        {
            "command": list(command),
            "compiler_digest": compiler_digest,
            "compiler_executable": compiler_executable,
            "launcher_digest": launcher_digest,
            "launcher_executable": launcher_executable,
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class RustToolchain:
    """One ``rustc`` route with exact launcher and selected-compiler identity.

    Rustup commonly exposes ``rustc`` as a launcher symlink. Binding only that
    launcher's bytes would miss a change to the selected compiler, so this value also
    binds the compiler beneath the reported sysroot. Both files and the command's
    verbose version and sysroot selection are checked immediately before and after
    compilation. These temporal checks cannot make filesystem lookup and process
    creation atomic.
    """

    command: tuple[str, ...]
    launcher_executable: str
    launcher_digest: str
    compiler_executable: str
    compiler_digest: str
    version: str
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
            or not isinstance(self.launcher_executable, str)
            or not self.launcher_executable
            or not isinstance(self.compiler_executable, str)
            or not self.compiler_executable
            or not isinstance(self.version, str)
            or not self.version
        ):
            raise ValueError("Rust toolchain must identify a versioned rustc command")
        _require_sha256(self.launcher_digest, "Rust launcher identity")
        _require_sha256(self.compiler_digest, "Rust compiler identity")
        object.__setattr__(
            self,
            "identity",
            _rust_toolchain_identity(
                self.command,
                self.launcher_executable,
                self.launcher_digest,
                self.compiler_executable,
                self.compiler_digest,
                self.version,
            ),
        )

    def _require_files_unchanged(self) -> None:
        configured = Path(self.command[0])
        try:
            launcher = configured.resolve(strict=True)
            compiler = Path(self.compiler_executable).resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.rust_toolchain_changed",
                "Rust compiler became unavailable after toolchain selection",
            ) from exc
        if (
            str(launcher) != self.launcher_executable
            or str(compiler) != self.compiler_executable
            or not launcher.is_file()
            or not compiler.is_file()
        ):
            raise BuildError(
                "builder.rust_toolchain_changed",
                "Rust launcher or compiler path changed after toolchain selection",
            )
        digests = {launcher: executable_file_digest(launcher)}
        if compiler not in digests:
            digests[compiler] = executable_file_digest(compiler)
        if (
            digests[launcher] != self.launcher_digest
            or digests[compiler] != self.compiler_digest
        ):
            raise BuildError(
                "builder.rust_toolchain_changed",
                "Rust launcher or compiler bytes changed after toolchain selection",
            )

    def require_unchanged(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        timeout_seconds: float = DEFAULT_RUST_VERSION_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_RUSTC_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_RUSTC_STDERR_LIMIT_BYTES,
    ) -> None:
        configured = dict(os.environ if environment is None else environment)
        self._require_files_unchanged()
        version, compiler = _probe_rust_command(
            self.command,
            configured,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            unavailable_code="builder.rust_toolchain_changed",
        )
        self._require_files_unchanged()
        if version != self.version or str(compiler) != self.compiler_executable:
            raise BuildError(
                "builder.rust_toolchain_changed",
                "Rust version or selected sysroot changed after toolchain selection",
            )


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


def _probe_rust_command(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    unavailable_code: str,
) -> tuple[str, Path]:
    version_result = _run_bounded_process(
        [*command, "--version", "--verbose"],
        cwd=None,
        environment=environment,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        error_prefix="builder.rust_version",
    )
    version = (
        (version_result.stdout + version_result.stderr)
        .decode("utf-8", errors="replace")
        .strip()
    )
    if version_result.returncode != 0 or not version:
        raise BuildError(unavailable_code, "rustc did not report a usable version")

    sysroot_result = _run_bounded_process(
        [*command, "--print", "sysroot"],
        cwd=None,
        environment=environment,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        error_prefix="builder.rust_sysroot",
    )
    try:
        sysroot = sysroot_result.stdout.decode("utf-8").strip()
    except UnicodeError as exc:
        raise BuildError(unavailable_code, "rustc reported an invalid sysroot") from exc
    if (
        sysroot_result.returncode != 0
        or not sysroot
        or sysroot_result.stderr
        or "\n" in sysroot
        or "\r" in sysroot
    ):
        raise BuildError(unavailable_code, "rustc did not report one usable sysroot")
    compiler_name = "rustc.exe" if os.name == "nt" else "rustc"
    try:
        compiler = (Path(sysroot) / "bin" / compiler_name).resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            unavailable_code, "rustc's selected compiler executable is unavailable"
        ) from exc
    if not compiler.is_file():
        raise BuildError(
            unavailable_code,
            "rustc's selected compiler executable is not a regular file",
        )
    return version, compiler


def discover_rust_toolchain(
    environment: Mapping[str, str] | None = None,
    *,
    timeout_seconds: float = DEFAULT_RUST_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_RUSTC_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_RUSTC_STDERR_LIMIT_BYTES,
) -> RustToolchain:
    """Resolve ``RUSTC`` or ``rustc`` from the caller's explicit ``PATH``."""

    configured = dict(os.environ if environment is None else environment)
    raw = configured.get("RUSTC", "").strip()
    if raw:
        try:
            parsed = tuple(shlex.split(raw, posix=os.name != "nt"))
        except ValueError as exc:
            raise BuildError(
                "builder.rust_toolchain", "RUSTC is not a valid command"
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
            raise BuildError("builder.rust_toolchain", "RUSTC is empty")
    else:
        parsed = ("rustc",)

    executable = shutil.which(parsed[0], path=configured.get("PATH"))
    if executable is None:
        raise BuildError(
            "builder.rust_toolchain_unavailable",
            "rustc was not found through RUSTC or PATH",
        )
    invocation_path = Path(os.path.abspath(executable))
    try:
        resolved_executable = invocation_path.resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.rust_toolchain_unavailable",
            "rustc became unavailable during toolchain discovery",
        ) from exc
    if not resolved_executable.is_file():
        raise BuildError(
            "builder.rust_toolchain_unavailable",
            "rustc does not resolve to a regular executable file",
        )
    command = (str(invocation_path), *parsed[1:])
    launcher_digest = executable_file_digest(resolved_executable)
    try:
        version, compiler = _probe_rust_command(
            command,
            configured,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            unavailable_code="builder.rust_toolchain_unavailable",
        )
        compiler_digest = executable_file_digest(compiler)
    finally:
        try:
            current_executable = invocation_path.resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.rust_toolchain_changed",
                "Rust compiler became unavailable during toolchain discovery",
            ) from exc
        if (
            current_executable != resolved_executable
            or executable_file_digest(current_executable) != launcher_digest
        ):
            raise BuildError(
                "builder.rust_toolchain_changed",
                "Rust compiler bytes changed during toolchain discovery",
            )
    if executable_file_digest(compiler) != compiler_digest:
        raise BuildError(
            "builder.rust_toolchain_changed",
            "Rust compiler bytes changed during toolchain discovery",
        )
    return RustToolchain(
        command,
        str(resolved_executable),
        launcher_digest,
        str(compiler),
        compiler_digest,
        version,
    )


@dataclass(frozen=True, slots=True)
class RustBuildArtifact:
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


def _makefile_words(value: str) -> tuple[str, ...]:
    """Decode the filename escaping rustc uses on dep-info rule inputs.

    rustc emits Makefile-style dependency rules. In dependency positions it escapes
    spaces, tabs, ``#``, and other Makefile-special characters with a backslash. A
    backslash before an ordinary character is retained so native Windows separators
    do not lose information if rustc emits them rather than forward slashes.
    """

    words: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character in " \t":
            if current:
                words.append("".join(current))
                current = []
            index += 1
            continue
        if character != "\\":
            current.append(character)
            index += 1
            continue
        if index + 1 == len(value):
            raise BuildError(
                "builder.rust_dependency_manifest_invalid",
                "rustc emitted a dependency path with a dangling escape",
            )
        escaped = value[index + 1]
        if escaped in " \t#:\\":
            current.append(escaped)
            index += 2
        else:
            current.append("\\")
            index += 1
    if current:
        words.append("".join(current))
    return tuple(words)


def _rustc_dependency_tokens(dependency_file: Path) -> tuple[str, ...]:
    if dependency_file.is_symlink() or not dependency_file.is_file():
        raise BuildError(
            "builder.rust_dependency_manifest_invalid",
            "rustc did not produce a regular dependency manifest",
        )
    try:
        contents = dependency_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BuildError(
            "builder.rust_dependency_manifest_invalid",
            "rustc produced an unreadable dependency manifest",
        ) from exc

    # A backslash followed by a newline is Makefile line continuation, not part of
    # a path. rustc currently emits one rule per line, but accepting continuation
    # keeps this parser correct if a large dependency set is wrapped in the future.
    contents = contents.replace("\\\r\n", " ").replace("\\\n", " ")
    dependencies: set[str] = set()
    for line in contents.splitlines():
        separator: int | None = None
        escaped = False
        for index, character in enumerate(line):
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == ":" and (
                index + 1 == len(line) or line[index + 1] in " \t"
            ):
                separator = index
                break
        if separator is None:
            if line.strip():
                raise BuildError(
                    "builder.rust_dependency_manifest_invalid",
                    "rustc emitted a malformed dependency rule",
                )
            continue
        dependencies.update(_makefile_words(line[separator + 1 :]))
    if not dependencies:
        raise BuildError(
            "builder.rust_dependency_manifest_invalid",
            "rustc dependency manifest did not identify any compiler inputs",
        )
    return tuple(sorted(dependencies))


def _rustc_consumed_source_files(
    source_root: Path,
    dependency_file: Path,
    *,
    allowed_source_root: Path,
) -> tuple[str, ...]:
    """Resolve rustc dep-info to canonical generated-tree paths, fail closed."""

    source_root = source_root.resolve(strict=True)
    allowed_source_root = allowed_source_root.resolve(strict=True)
    try:
        allowed_source_root.relative_to(source_root)
    except ValueError as exc:
        raise BuildError(
            "builder.rust_dependency_role_invalid",
            "Rust source role is outside the authorized generated tree",
        ) from exc

    consumed: set[str] = set()
    for token in _rustc_dependency_tokens(dependency_file):
        raw_path = Path(token)
        candidate = raw_path if raw_path.is_absolute() else source_root / raw_path
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.rust_dependency_manifest_invalid",
                f"rustc reported an unavailable compiler input: {token}",
            ) from exc
        if not resolved.is_file():
            raise BuildError(
                "builder.rust_dependency_manifest_invalid",
                f"rustc reported a non-file compiler input: {token}",
            )
        try:
            relative = resolved.relative_to(source_root)
        except ValueError as exc:
            raise BuildError(
                "builder.rust_dependency_outside_source",
                f"Rust compilation consumed a file outside generated source: {token}",
            ) from exc
        try:
            resolved.relative_to(allowed_source_root)
        except ValueError as exc:
            raise BuildError(
                "builder.rust_dependency_outside_role",
                "Rust compilation crossed its generated source role boundary: "
                f"{relative.as_posix()}",
            ) from exc
        consumed.add(relative.as_posix())
    return tuple(sorted(consumed))


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


class GuardedRustBuilder:
    """Compile a generated Rust binary after exact build authorization checks."""

    builder_id = "builder:rust-native@1"

    def __init__(
        self,
        toolchain: RustToolchain,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        timeout_seconds: float = DEFAULT_RUST_BUILD_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_RUSTC_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_RUSTC_STDERR_LIMIT_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Rust build timeout must be positive")
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("Rust compiler output limits must be positive")
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
    ) -> RustBuildArtifact:
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
                "Build request selected another Rust toolchain",
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
        crate_root = resolved_source / "source" / "main.rs"
        if crate_root.is_symlink() or not crate_root.is_file():
            raise BuildError(
                "builder.rust_entrypoint_missing",
                "Generated Rust source must provide source/main.rs",
            )

        destination_root = artifact_store.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="rust-build-", dir=destination_root))
        compiler_output = Path(tempfile.mkdtemp(prefix="literate-rustc-"))
        executable_name = "sample.exe" if os.name == "nt" else "sample"
        compiled_executable = compiler_output / executable_name
        executable = staging / executable_name
        dependency_file = compiler_output / "sample.d"
        command = [
            *self.toolchain.command,
            "--edition=2021",
            "--crate-name",
            "sample",
            "--emit=link,dep-info",
            "--out-dir",
            str(compiler_output),
            "-C",
            "opt-level=2",
            "source/main.rs",
        ]
        try:
            try:
                completed = _run_bounded_process(
                    command,
                    cwd=resolved_source,
                    environment=dict(os.environ),
                    timeout_seconds=self.timeout_seconds,
                    stdout_limit_bytes=self.stdout_limit_bytes,
                    stderr_limit_bytes=self.stderr_limit_bytes,
                    error_prefix="builder.rust_compile",
                )
            finally:
                self.toolchain.require_unchanged()
                _require_source_tree_unchanged(resolved_source, actual_source_digest)
            if (
                completed.returncode != 0
                or not compiled_executable.is_file()
                or not dependency_file.is_file()
            ):
                detail = (completed.stdout + completed.stderr)[-4000:].decode(
                    "utf-8", errors="replace"
                )
                raise BuildError(
                    "builder.rust_compile_failed", "Rust compilation failed: " + detail
                )

            consumed_source_files = _rustc_consumed_source_files(
                resolved_source,
                dependency_file,
                allowed_source_root=crate_root.parent,
            )
            dependency_file.unlink()
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
            if not _cached_rust_artifact_matches(
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
                if not _cached_rust_artifact_matches(
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
            return RustBuildArtifact(
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


def _cached_rust_artifact_matches(
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
    "DEFAULT_RUST_BUILD_TIMEOUT_SECONDS",
    "DEFAULT_RUST_VERSION_TIMEOUT_SECONDS",
    "DEFAULT_RUSTC_STDERR_LIMIT_BYTES",
    "DEFAULT_RUSTC_STDOUT_LIMIT_BYTES",
    "GuardedRustBuilder",
    "RustBuildArtifact",
    "RustToolchain",
    "discover_rust_toolchain",
]
