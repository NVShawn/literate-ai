"""One authorization-gated build for a Rust backend and JavaScript frontend."""

from __future__ import annotations

import hashlib
import json
import os
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
from .javascript import (
    DEFAULT_JAVASCRIPT_BUILD_TIMEOUT_SECONDS,
    DEFAULT_NODE_STDERR_LIMIT_BYTES,
    DEFAULT_NODE_STDOUT_LIMIT_BYTES,
    NodeToolchain,
    controlled_node_environment,
    discover_node_toolchain,
)
from .python import (
    BuildError,
    _require_source_tree_unchanged,
    canonical_tree_digest,
    require_unsandboxed_host_build_authorization,
)
from .rust import (
    DEFAULT_RUST_BUILD_TIMEOUT_SECONDS,
    RustToolchain,
    _require_generated_files_covered,
    _rustc_consumed_source_files,
    discover_rust_toolchain,
)

_REQUIRED_ALLOWED_OUTPUTS = frozenset(
    {"javascript-checked-bundle", "native-executable"}
)


def _composite_toolchain_identity(rust: RustToolchain, node: NodeToolchain) -> str:
    document = {
        "compiler_identity": rust.identity,
        "runtime_identity": node.identity,
        "schema": "literate-ai/rust-javascript-toolchain@1",
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class RustJavaScriptToolchain:
    """Exact compiler/runtime pair selected for one full-stack build."""

    rust: RustToolchain
    node: NodeToolchain
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.rust, RustToolchain) or not isinstance(
            self.node, NodeToolchain
        ):
            raise TypeError(
                "Rust/JavaScript toolchain requires exact Rust and Node toolchains"
            )
        object.__setattr__(
            self, "identity", _composite_toolchain_identity(self.rust, self.node)
        )

    @property
    def compiler_identity(self) -> str:
        return self.rust.identity

    @property
    def runtime_identity(self) -> str:
        return self.node.identity

    def require_unchanged(self, environment: Mapping[str, str] | None = None) -> None:
        configured = dict(os.environ if environment is None else environment)
        self.rust.require_unchanged(configured)
        self.node.require_unchanged(configured)


def discover_rust_javascript_toolchain(
    environment: Mapping[str, str] | None = None,
) -> RustJavaScriptToolchain:
    """Discover and bind the Rust compiler and Node runtime from one environment."""

    configured = dict(os.environ if environment is None else environment)
    toolchain = RustJavaScriptToolchain(
        rust=discover_rust_toolchain(configured),
        node=discover_node_toolchain(configured),
    )
    toolchain.require_unchanged(configured)
    return toolchain


@dataclass(frozen=True, slots=True)
class RustJavaScriptBuildArtifact:
    artifact_digest: str
    artifact_path: Path
    source_bundle_digest: str
    authorization_id: str
    backend_executable_file: str
    frontend_entrypoint_file: str
    compiler_identity: str
    runtime_identity: str
    toolchain_identity: str
    runtime_command: tuple[str, ...]
    compiled_files: tuple[str, ...]
    consumed_backend_files: tuple[str, ...]
    checked_frontend_files: tuple[str, ...]
    lifecycle_consumed_files: tuple[str, ...]
    build_system_consumed_files: tuple[str, ...]
    build_input_consumption_identity: str | None


class GuardedRustJavaScriptBuilder:
    """Build both application roles under one exact request and authorization.

    This is intentionally not a coordinator for the standalone builders. The request
    binds the complete generated tree, the composite toolchain, and both output kinds;
    deriving role-scoped requests would weaken that authority boundary.
    """

    builder_id = "builder:rust-javascript-full-stack@1"
    backend_source_file = "source/backend/main.rs"
    frontend_source_file = "source/frontend/main.js"

    def __init__(
        self,
        toolchain: RustJavaScriptToolchain,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        rust_timeout_seconds: float = DEFAULT_RUST_BUILD_TIMEOUT_SECONDS,
        node_timeout_seconds: float = DEFAULT_JAVASCRIPT_BUILD_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_NODE_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_NODE_STDERR_LIMIT_BYTES,
    ) -> None:
        if not isinstance(toolchain, RustJavaScriptToolchain):
            raise TypeError("full-stack builder requires a composite toolchain")
        if rust_timeout_seconds <= 0 or node_timeout_seconds <= 0:
            raise ValueError("full-stack build timeouts must be positive")
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("full-stack build output limits must be positive")
        self.toolchain = toolchain
        self.authorization_verifier = (
            authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self.rust_timeout_seconds = rust_timeout_seconds
        self.node_timeout_seconds = node_timeout_seconds
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
    ) -> RustJavaScriptBuildArtifact:
        self.authorization_verifier.require_build_valid(authorization, request, now=now)
        require_unsandboxed_host_build_authorization(request, authorization)
        if request.builder_id != self.builder_id:
            raise BuildError(
                "builder.identity_mismatch", "Build request selected another builder"
            )
        missing_outputs = _REQUIRED_ALLOWED_OUTPUTS.difference(request.allowed_outputs)
        if missing_outputs:
            raise BuildError(
                "builder.output_not_authorized",
                "Rust/JavaScript build requires native executable and checked "
                "JavaScript bundle outputs",
            )
        if request.toolchain_digest != self.toolchain.identity:
            raise BuildError(
                "builder.toolchain_mismatch",
                "Build request selected another Rust/JavaScript toolchain",
            )

        environment = dict(os.environ)
        self.toolchain.require_unchanged(environment)
        resolved_source = source_root.resolve(strict=True)
        actual_source_digest = canonical_tree_digest(resolved_source)
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
        backend_source = resolved_source.joinpath(*Path(self.backend_source_file).parts)
        frontend_source = resolved_source.joinpath(
            *Path(self.frontend_source_file).parts
        )
        if backend_source.is_symlink() or not backend_source.is_file():
            raise BuildError(
                "builder.rust_entrypoint_missing",
                f"Generated full-stack source must provide {self.backend_source_file}",
            )
        if frontend_source.is_symlink() or not frontend_source.is_file():
            raise BuildError(
                "builder.javascript_entrypoint_missing",
                f"Generated full-stack source must provide {self.frontend_source_file}",
            )
        frontend_root = frontend_source.parent
        frontend_scripts = tuple(
            sorted(path for path in frontend_root.rglob("*.js") if path.is_file())
        )
        if any(path.is_symlink() for path in frontend_scripts):
            raise BuildError(
                "builder.source_symlink", "Frontend source symlinks are not allowed"
            )

        destination_root = artifact_store.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix="rust-javascript-build-", dir=destination_root)
        )
        backend_executable_file = (
            "backend/sample.exe" if os.name == "nt" else "backend/sample"
        )
        frontend_entrypoint_file = "frontend/main.js"
        backend_executable = staging.joinpath(*Path(backend_executable_file).parts)
        backend_executable.parent.mkdir(parents=True)
        try:
            consumed_backend_files = self._compile_backend(
                resolved_source,
                backend_executable,
                actual_source_digest,
                environment,
            )
            checked_frontend_files = tuple(
                script.relative_to(resolved_source).as_posix()
                for script in frontend_scripts
            )
            _require_generated_files_covered(
                resolved_source,
                consumed_backend_files,
                checked_frontend_files,
                lifecycle_consumed_files,
                build_system_consumed_files,
            )
            self._require_only_backend_output(staging, backend_executable_file)
            frontend_files = self._check_and_copy_frontend(
                resolved_source,
                frontend_root,
                frontend_scripts,
                staging,
                actual_source_digest,
                environment,
            )
            try:
                self.toolchain.require_unchanged(environment)
            finally:
                _require_source_tree_unchanged(resolved_source, actual_source_digest)

            compiled_files = (backend_executable_file, *frontend_files)
            file_records = [
                {
                    "digest": _file_digest(staging.joinpath(*Path(path).parts)),
                    "path": path,
                }
                for path in compiled_files
            ]
            manifest = {
                "authorization_id": authorization.authorization_id,
                "backend_executable_file": backend_executable_file,
                "builder_id": self.builder_id,
                "compiled_files": list(compiled_files),
                "compiler_identity": self.toolchain.compiler_identity,
                "consumed_backend_files": list(consumed_backend_files),
                "checked_frontend_files": list(checked_frontend_files),
                "lifecycle_consumed_files": list(lifecycle_consumed_files),
                "build_system_consumed_files": list(build_system_consumed_files),
                "build_input_consumption_identity": (
                    build_input_consumption.identity
                    if build_input_consumption is not None
                    else None
                ),
                "files": file_records,
                "frontend_entrypoint_file": frontend_entrypoint_file,
                "runtime_command": list(self.toolchain.node.command),
                "runtime_identity": self.toolchain.runtime_identity,
                "source_bundle_digest": actual_source_digest,
                "toolchain_identity": self.toolchain.identity,
            }
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            artifact_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            (staging / "build-manifest.json").write_bytes(manifest_bytes)
            if not _cached_composite_artifact_matches(
                staging, manifest=manifest, manifest_bytes=manifest_bytes
            ):
                raise BuildError(
                    "builder.artifact_invalid",
                    "Built artifact differs from its exact manifest",
                )
            final = destination_root / artifact_digest.removeprefix("sha256:")
            if final.exists():
                if not _cached_composite_artifact_matches(
                    final, manifest=manifest, manifest_bytes=manifest_bytes
                ):
                    raise BuildError(
                        "builder.artifact_collision", "Existing artifact differs"
                    )
                shutil.rmtree(staging)
            else:
                os.replace(staging, final)
            return RustJavaScriptBuildArtifact(
                artifact_digest=artifact_digest,
                artifact_path=final,
                source_bundle_digest=actual_source_digest,
                authorization_id=authorization.authorization_id,
                backend_executable_file=backend_executable_file,
                frontend_entrypoint_file=frontend_entrypoint_file,
                compiler_identity=self.toolchain.compiler_identity,
                runtime_identity=self.toolchain.runtime_identity,
                toolchain_identity=self.toolchain.identity,
                runtime_command=self.toolchain.node.command,
                compiled_files=compiled_files,
                consumed_backend_files=consumed_backend_files,
                checked_frontend_files=checked_frontend_files,
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

    def _compile_backend(
        self,
        source_root: Path,
        executable: Path,
        source_digest: str,
        environment: dict[str, str],
    ) -> tuple[str, ...]:
        compiler_output = Path(tempfile.mkdtemp(prefix="literate-rustc-"))
        compiled_executable = compiler_output / executable.name
        dependency_file = compiler_output / "sample.d"
        self.toolchain.rust.require_unchanged(environment)
        try:
            try:
                completed = _run_bounded_process(
                    [
                        *self.toolchain.rust.command,
                        "--edition=2021",
                        "--crate-name",
                        "sample",
                        "--emit=link,dep-info",
                        "--out-dir",
                        str(compiler_output),
                        "-C",
                        "opt-level=2",
                        self.backend_source_file,
                    ],
                    cwd=source_root,
                    environment=environment,
                    timeout_seconds=self.rust_timeout_seconds,
                    stdout_limit_bytes=self.stdout_limit_bytes,
                    stderr_limit_bytes=self.stderr_limit_bytes,
                    error_prefix="builder.rust_javascript_compile",
                )
            finally:
                try:
                    self.toolchain.rust.require_unchanged(environment)
                finally:
                    _require_source_tree_unchanged(source_root, source_digest)
            if (
                completed.returncode != 0
                or not compiled_executable.is_file()
                or not dependency_file.is_file()
            ):
                detail = (completed.stdout + completed.stderr)[-4000:].decode(
                    "utf-8", errors="replace"
                )
                raise BuildError(
                    "builder.rust_javascript_compile_failed",
                    "Rust backend compilation failed: " + detail,
                )
            consumed = _rustc_consumed_source_files(
                source_root,
                dependency_file,
                allowed_source_root=source_root / "source" / "backend",
            )
            shutil.copy2(compiled_executable, executable)
            return consumed
        finally:
            shutil.rmtree(compiler_output, ignore_errors=True)

    @staticmethod
    def _require_only_backend_output(root: Path, executable_file: str) -> None:
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        if actual != {executable_file}:
            raise BuildError(
                "builder.unexpected_output",
                "Rust compiler produced files outside the authorized artifact tree",
            )

    def _check_and_copy_frontend(
        self,
        source_root: Path,
        frontend_root: Path,
        scripts: tuple[Path, ...],
        staging: Path,
        source_digest: str,
        environment: dict[str, str],
    ) -> tuple[str, ...]:
        controlled_environment = controlled_node_environment(environment)
        copied: list[str] = []
        for script in scripts:
            relative_source = script.relative_to(source_root).as_posix()
            self.toolchain.node.require_unchanged(controlled_environment)
            try:
                completed = _run_bounded_process(
                    [*self.toolchain.node.command, "--check", relative_source],
                    cwd=source_root,
                    environment=controlled_environment,
                    timeout_seconds=self.node_timeout_seconds,
                    stdout_limit_bytes=self.stdout_limit_bytes,
                    stderr_limit_bytes=self.stderr_limit_bytes,
                    error_prefix="builder.rust_javascript_frontend_check",
                )
            finally:
                try:
                    self.toolchain.node.require_unchanged(controlled_environment)
                finally:
                    _require_source_tree_unchanged(source_root, source_digest)
            if completed.returncode != 0:
                detail = (completed.stdout + completed.stderr)[-4000:].decode(
                    "utf-8", errors="replace"
                )
                raise BuildError(
                    "builder.rust_javascript_frontend_check_failed",
                    "JavaScript frontend syntax check failed: " + detail,
                )
            relative_frontend = script.relative_to(frontend_root).as_posix()
            artifact_file = f"frontend/{relative_frontend}"
            target = staging.joinpath(*Path(artifact_file).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(script.read_bytes())
            copied.append(artifact_file)
            _require_source_tree_unchanged(source_root, source_digest)
        return tuple(copied)


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _cached_composite_artifact_matches(
    root: Path, *, manifest: dict[str, object], manifest_bytes: bytes
) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        return False
    expected = {
        str(item["path"]): str(item["digest"])
        for item in raw_files
        if isinstance(item, dict) and set(item) == {"digest", "path"}
    }
    if len(expected) != len(raw_files):
        return False
    actual: dict[str, str] = {}
    manifest_found = False
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            return False
        if path.is_dir():
            continue
        if not path.is_file():
            return False
        relative = path.relative_to(root).as_posix()
        if relative == "build-manifest.json":
            manifest_found = path.read_bytes() == manifest_bytes
            continue
        actual[relative] = _file_digest(path)
    return manifest_found and actual == expected


__all__ = [
    "GuardedRustJavaScriptBuilder",
    "RustJavaScriptBuildArtifact",
    "RustJavaScriptToolchain",
    "discover_rust_javascript_toolchain",
]
