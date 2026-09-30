"""Authorization-gated Swift executable builder."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
)

from ._process import run_bounded_process
from .cpp import CppToolchain, NativeBuildArtifact
from .python import (
    BuildError,
    _require_source_tree_unchanged,
    canonical_tree_digest,
    require_unsandboxed_host_build_authorization,
)

DEFAULT_SWIFT_BUILD_TIMEOUT_SECONDS = 120.0
DEFAULT_SWIFTC_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_SWIFTC_STDERR_LIMIT_BYTES = 1024 * 1024


class GuardedSwiftBuilder:
    """Compile a generated Swift tree after exact authorization checks pass.

    Swift discovery currently uses the shared executable-identity toolchain record;
    the record is language-neutral in behavior even though its historical type name
    is ``CppToolchain``.
    """

    builder_id = "builder:swift-native@1"

    def __init__(
        self,
        toolchain: CppToolchain,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        timeout_seconds: float = DEFAULT_SWIFT_BUILD_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_SWIFTC_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_SWIFTC_STDERR_LIMIT_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Swift build timeout must be positive")
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("Swift compiler output limits must be positive")
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
    ) -> NativeBuildArtifact:
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
                "Build request selected another Swift toolchain",
            )
        self.toolchain.require_unchanged()
        source_digest = canonical_tree_digest(source_root)
        if source_digest != request.source_bundle_digest:
            raise BuildError(
                "builder.source_digest_mismatch",
                "Source tree changed after authorization",
            )

        resolved_source = source_root.resolve(strict=True)
        sources = tuple(
            sorted(path for path in resolved_source.rglob("*.swift") if path.is_file())
        )
        if not sources:
            raise BuildError(
                "builder.swift_entrypoint_missing",
                "Generated Swift source has no source files",
            )
        if any(path.is_symlink() for path in sources):
            raise BuildError("builder.source_symlink", "Swift source symlink rejected")

        destination = artifact_store.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="swift-build-", dir=destination))
        executable_name = "sample.exe" if os.name == "nt" else "sample"
        executable = staging / executable_name
        relative_sources = [
            path.relative_to(resolved_source).as_posix() for path in sources
        ]
        try:
            try:
                completed = run_bounded_process(
                    [
                        *self.toolchain.command,
                        "-O",
                        *relative_sources,
                        "-o",
                        str(executable),
                    ],
                    cwd=resolved_source,
                    environment=dict(os.environ),
                    timeout_seconds=self.timeout_seconds,
                    stdout_limit_bytes=self.stdout_limit_bytes,
                    stderr_limit_bytes=self.stderr_limit_bytes,
                    error_prefix="builder.swift_compile",
                )
            finally:
                self.toolchain.require_unchanged()
            _require_source_tree_unchanged(resolved_source, source_digest)
            if completed.returncode != 0 or not executable.is_file():
                detail = (completed.stdout + completed.stderr)[-4000:].decode(
                    "utf-8", errors="replace"
                )
                raise BuildError(
                    "builder.swift_generated_source_rejected",
                    "Swift compiler rejected the generated source: " + detail,
                )
            executable_digest = (
                f"sha256:{hashlib.sha256(executable.read_bytes()).hexdigest()}"
            )
            manifest = {
                "authorization_id": authorization.authorization_id,
                "builder_id": self.builder_id,
                "compiler_identity": self.toolchain.identity,
                "executable_digest": executable_digest,
                "executable_file": executable_name,
                "source_bundle_digest": source_digest,
            }
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            artifact_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            (staging / "build-manifest.json").write_bytes(manifest_bytes)
            if not _cached_swift_artifact_matches(
                staging, executable_name, executable_digest, manifest_bytes
            ):
                raise BuildError(
                    "builder.artifact_invalid",
                    "Built Swift artifact differs from its exact manifest",
                )
            final = destination / artifact_digest.removeprefix("sha256:")
            if final.exists():
                if not _cached_swift_artifact_matches(
                    final, executable_name, executable_digest, manifest_bytes
                ):
                    raise BuildError(
                        "builder.artifact_collision", "Existing artifact differs"
                    )
                shutil.rmtree(staging)
            else:
                os.replace(staging, final)
            return NativeBuildArtifact(
                artifact_digest,
                final,
                source_digest,
                authorization.authorization_id,
                executable_name,
                self.toolchain.identity,
            )
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise


def _cached_swift_artifact_matches(
    root: Path,
    executable_name: str,
    executable_digest: str,
    manifest_bytes: bytes,
) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            return False
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            return False
    executable = root / executable_name
    return (
        files == {"build-manifest.json", executable_name}
        and (root / "build-manifest.json").read_bytes() == manifest_bytes
        and f"sha256:{hashlib.sha256(executable.read_bytes()).hexdigest()}"
        == executable_digest
    )


__all__ = [
    "DEFAULT_SWIFT_BUILD_TIMEOUT_SECONDS",
    "DEFAULT_SWIFTC_STDERR_LIMIT_BYTES",
    "DEFAULT_SWIFTC_STDOUT_LIMIT_BYTES",
    "GuardedSwiftBuilder",
]
