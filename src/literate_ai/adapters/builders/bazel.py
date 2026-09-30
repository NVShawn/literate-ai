"""Authorized Bazel conformance and Bzlmod dependency-evidence decorator.

This adapter deliberately does not claim that the delegate artifact was produced by
Bazel.  It proves that the exact generated tree resolves and builds through Bazel 9 in
a disposable projection and invokes the selected native builder with the unchanged
request and authorization.  It never runs generated programs or tests: execution is a
later lifecycle phase, after the resolved SBOM gate.  The current build-request wire
can bind only one toolchain, so the delegate toolchain remains the authorized build
toolchain while the exact Bazel toolchain is evidence-bound as the dependency resolver
toolchain.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Protocol

from literate_ai.contracts import (
    ArtifactMaterializationPlan,
    ComponentBuildManifest,
    CompositeBuildRequest,
    ContentIdentity,
    canonical_identity,
    canonical_relative_posix_path,
)
from literate_ai.ports import (
    BuildDependencyEvidenceArtifact,
    BuildDependencyObservation,
    BuildInputConsumption,
    BuildInputConsumptionAwareBuilder,
    BzlmodModule,
    BzlmodRootModule,
    require_build_result,
)
from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
)

from ._process import BoundedProcessResult
from ._process import run_bounded_process as _run_bounded_process
from .composite import (
    CompositeBuildDelegate,
    validate_composite_build_invocation,
)
from .python import (
    BuildError,
    canonical_tree_digest,
    executable_file_digest,
    require_unsandboxed_host_build_authorization,
)

BAZEL_DEPENDENCY_EVIDENCE_OUTPUT = "bazel-dependency-evidence"
DEFAULT_BAZEL_VERSION_TIMEOUT_SECONDS = 30.0
DEFAULT_BAZEL_COMMAND_TIMEOUT_SECONDS = 30.0 * 60.0
DEFAULT_BAZEL_STDOUT_LIMIT_BYTES = 64 * 1024 * 1024
_BAZEL_TEMPORARY_DIRECTORY_PREFIX = "lb-"
_BAZEL_CLEANUP_RETRY_DELAYS_SECONDS = (
    0.0,
    0.05,
    0.1,
    0.2,
    0.4,
    0.8,
    1.6,
    3.2,
    6.4,
    12.8,
)
DEFAULT_BAZEL_STDERR_LIMIT_BYTES = 8 * 1024 * 1024
BZLMOD_RESOLVER_ID = "bazel/bzlmod@1"

_BAZEL_VERSION = re.compile(
    r"^bazel (?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)(?:[A-Za-z0-9._+-]*)$"
)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_BAZELISK_CACHE_ENTRIES = 64
_GRAPH_NODE_FIELDS = frozenset(
    {
        "key",
        "name",
        "version",
        "apparentName",
        "dependencies",
        "indirectDependencies",
        "cycles",
        "root",
        "unexpanded",
    }
)
_REPOSITORY_FIELDS = frozenset(
    {
        "canonicalName",
        "repoRuleName",
        "repoRuleBzlLabel",
        "apparentName",
        "moduleKey",
        "originalName",
        "attribute",
    }
)
# Bazel 9's well-known-module mapping intentionally omits the normal ``+`` suffix
# for this exact registry module.  This is not a fuzzy fallback for arbitrary names.
_WELL_KNOWN_UNVERSIONED_MODULE_REPOSITORIES = frozenset({"platforms"})


@contextmanager
def _temporary_bazel_directory() -> Iterator[str]:
    """Remove a Bazel projection after bounded retries for transient host locks."""

    root = Path(tempfile.mkdtemp(prefix=_BAZEL_TEMPORARY_DIRECTORY_PREFIX))
    try:
        yield str(root)
    finally:
        _remove_bazel_directory(root)


def _remove_bazel_read_only(
    function: Callable[..., object],
    path: str,
    error: tuple[type[BaseException], BaseException, TracebackType | None],
) -> None:
    """Clear only a read-only output bit; active locks and other errors still fail."""

    if not isinstance(error[1], PermissionError):
        raise error[1]
    for candidate in (Path(path), Path(path).parent):
        mode = candidate.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue
        owner_access = stat.S_IRUSR | stat.S_IWUSR
        if stat.S_ISDIR(mode):
            owner_access |= stat.S_IXUSR
        os.chmod(candidate, mode | owner_access)
    if function in {os.open, os.scandir}:
        # Python 3.14 reports a traversal denial through os.open.  rmtree's
        # outer bounded retry must reopen the directory after permissions are
        # repaired; calling os.open(path) here is both invalid and leaks a fd.
        return
    function(path)


def _bazel_removal_path(root: Path) -> str:
    """Use Win32's extended-length namespace for derived Bazel teardown only."""

    value = str(root.absolute())
    if sys.platform != "win32" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value.lstrip("\\")
    return "\\\\?\\" + value


def _remove_bazel_directory(root: Path) -> None:
    failure: OSError | None = None
    removal_path = _bazel_removal_path(root)
    for delay in _BAZEL_CLEANUP_RETRY_DELAYS_SECONDS:
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(removal_path, onerror=_remove_bazel_read_only)
        except FileNotFoundError:
            return
        except OSError as exc:
            failure = exc
        else:
            return
    raise BuildError(
        "builder.bazel_cleanup_failed",
        "Bazel temporary output remained locked after bounded cleanup retries",
    ) from failure


class _DelegateBuilder(Protocol):
    builder_id: str

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class BazelDependencyEvidence:
    """Exact Bzlmod observation and immutable evidence produced before compilation."""

    observation: BuildDependencyObservation
    lock_bytes: bytes
    graph_bytes: bytes
    repository_bytes: bytes


@dataclass(frozen=True, slots=True)
class BazelBuildInputEvidence:
    """Exact Bazel local-input closure consumed by one frozen source tree."""

    consumption: BuildInputConsumption
    buildfiles_bytes: bytes
    source_inputs_bytes: bytes


def _command_output_bytes(result: object, field_name: str) -> bytes:
    value = getattr(result, field_name, None)
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise BuildError(
        "builder.bazel_output_invalid",
        f"Bazel {field_name} output is not bounded text bytes",
    )


def collect_bzlmod_dependency_evidence(
    *,
    run: Callable[[str, Sequence[str]], object],
    workspace: Path,
    source_bundle_digest: str,
    build_toolchain_identity: str,
    resolver_toolchain_identity: str,
) -> BazelDependencyEvidence:
    """Resolve, freeze, and re-read one complete Bzlmod graph.

    The caller owns process isolation and must make ``run`` fail on any nonzero result.
    Both the conformance decorator and the Standard Bazel executor use this exact
    projection so dependency semantics cannot drift between lifecycle paths.
    """

    run(
        "resolve_graph",
        ("mod", "graph", "--output=json", "--cycles", "--lockfile_mode=update"),
    )
    run(
        "resolve_repositories",
        (
            "mod",
            "show_repo",
            "--all_repos",
            "--output=streamed_jsonproto",
            "--lockfile_mode=update",
        ),
    )
    lock_path = workspace / "MODULE.bazel.lock"
    if lock_path.is_symlink() or not lock_path.is_file():
        raise BuildError(
            "builder.bazel_lock_missing", "Bazel did not produce MODULE.bazel.lock"
        )
    lock_bytes = lock_path.read_bytes()
    lock_document = _strict_json(lock_bytes, label="MODULE.bazel.lock")
    if (
        not isinstance(lock_document, Mapping)
        or type(lock_document.get("lockFileVersion")) is not int
        or lock_document["lockFileVersion"] <= 0
    ):
        raise BuildError(
            "builder.bazel_lock_invalid",
            "MODULE.bazel.lock has no supported version",
        )
    graph_result = run(
        "graph", ("mod", "graph", "--output=json", "--cycles", "--lockfile_mode=error")
    )
    graph_bytes = _command_output_bytes(graph_result, "stdout")
    _, root_module, graph_modules = _normalized_module_graph(graph_bytes)
    repository_result = run(
        "repositories",
        (
            "mod",
            "show_repo",
            "--all_repos",
            "--output=streamed_jsonproto",
            "--lockfile_mode=error",
        ),
    )
    repository_bytes = _command_output_bytes(repository_result, "stdout")
    _normalized_repositories(repository_bytes, modules=graph_modules)
    evidence_contents = (
        ("bazel-module-lock", ".literate/bazel/MODULE.bazel.lock", lock_bytes),
        ("bazel-module-graph", ".literate/bazel/module-graph.json", graph_bytes),
        (
            "bazel-repository-definitions",
            ".literate/bazel/repositories.ndjson",
            repository_bytes,
        ),
    )
    observation = BuildDependencyObservation(
        resolver_id=BZLMOD_RESOLVER_ID,
        source_bundle_digest=source_bundle_digest,
        build_toolchain_identity=build_toolchain_identity,
        resolver_toolchain_identity=resolver_toolchain_identity,
        root_module=root_module,
        modules=graph_modules,
        evidence_artifacts=tuple(
            BuildDependencyEvidenceArtifact(
                kind=kind,
                logical_name=relative,
                content_identity=_sha256(content),
            )
            for kind, relative, content in evidence_contents
        ),
    )
    return BazelDependencyEvidence(
        observation, lock_bytes, graph_bytes, repository_bytes
    )


def collect_bazel_build_input_evidence(
    *,
    run: Callable[[str, Sequence[str]], object],
    projection: Path,
    workspace: Path,
    generated_files: Mapping[str, bytes],
    source_bundle_digest: str,
    target_expression: str = "//...",
) -> BazelBuildInputEvidence:
    """Analyze and bind Bazel's complete local source inputs before compilation."""

    run(
        "analyze",
        (
            "build",
            "--nobuild",
            "--lockfile_mode=error",
            target_expression,
        ),
    )
    buildfiles_result = run(
        "build_inputs",
        (
            "query",
            "--lockfile_mode=error",
            "--output=label",
            f"buildfiles({target_expression})",
        ),
    )
    source_inputs_result = run(
        "source_inputs",
        (
            "query",
            "--lockfile_mode=error",
            "--output=label",
            f'kind("source file", deps({target_expression}))',
        ),
    )
    buildfiles_bytes = _command_output_bytes(buildfiles_result, "stdout")
    source_inputs_bytes = _command_output_bytes(source_inputs_result, "stdout")
    return BazelBuildInputEvidence(
        consumption=_build_input_consumption(
            buildfiles_bytes,
            source_inputs_bytes,
            projection=projection,
            workspace=workspace,
            generated_files=generated_files,
            source_bundle_digest=source_bundle_digest,
        ),
        buildfiles_bytes=buildfiles_bytes,
        source_inputs_bytes=source_inputs_bytes,
    )


@dataclass(frozen=True, slots=True)
class _CompositeInvocation:
    request: CompositeBuildRequest
    manifest: ComponentBuildManifest
    materialization_plan: ArtifactMaterializationPlan
    artifact: Mapping[str, object]


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _toolchain_identity(
    *,
    command: tuple[str, ...],
    launcher_executable: str,
    launcher_digest: str,
    version: str,
) -> str:
    return _sha256(
        _canonical_json(
            {
                "command": list(command),
                "launcher_digest": launcher_digest,
                "launcher_executable": launcher_executable,
                "version": version,
            }
        )
    )


@dataclass(frozen=True, slots=True)
class BazelToolchain:
    """One Bazel 9 command with exact launcher bytes and reported version."""

    command: tuple[str, ...]
    launcher_executable: str
    launcher_digest: str
    version: str
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
            or not isinstance(self.launcher_executable, str)
            or not self.launcher_executable
            or not isinstance(self.version, str)
            or _BAZEL_VERSION.fullmatch(self.version) is None
        ):
            raise ValueError("Bazel toolchain requires one exact Bazel 9 command")
        match = _BAZEL_VERSION.fullmatch(self.version)
        assert match is not None
        if int(match.group("major")) != 9:
            raise ValueError("Bazel toolchain requires Bazel major version 9")
        encoded = self.launcher_digest.removeprefix("sha256:")
        if (
            not self.launcher_digest.startswith("sha256:")
            or len(encoded) != 64
            or encoded != encoded.casefold()
        ):
            raise ValueError("Bazel launcher identity must be a sha256 digest")
        try:
            int(encoded, 16)
        except ValueError as exc:
            raise ValueError("Bazel launcher identity must be a sha256 digest") from exc
        object.__setattr__(
            self,
            "identity",
            _toolchain_identity(
                command=self.command,
                launcher_executable=self.launcher_executable,
                launcher_digest=self.launcher_digest,
                version=self.version,
            ),
        )

    def require_unchanged(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        cwd: Path | None = None,
        timeout_seconds: float = DEFAULT_BAZEL_VERSION_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_BAZEL_STDERR_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_BAZEL_STDERR_LIMIT_BYTES,
    ) -> None:
        configured = dict(os.environ if environment is None else environment)
        try:
            launcher = Path(self.command[0]).resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.bazel_toolchain_changed",
                "Bazel launcher became unavailable after selection",
            ) from exc
        if str(launcher) != self.launcher_executable or not launcher.is_file():
            raise BuildError(
                "builder.bazel_toolchain_changed",
                "Bazel launcher path changed after selection",
            )
        if executable_file_digest(launcher) != self.launcher_digest:
            raise BuildError(
                "builder.bazel_toolchain_changed",
                "Bazel launcher bytes changed after selection",
            )
        current = _probe_bazel(
            self.command,
            configured,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
        )
        if current != self.version:
            raise BuildError(
                "builder.bazel_toolchain_changed",
                "Bazel version changed after toolchain selection",
            )
        if executable_file_digest(launcher) != self.launcher_digest:
            raise BuildError(
                "builder.bazel_toolchain_changed",
                "Bazel launcher bytes changed during version validation",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "launcher_executable": self.launcher_executable,
            "launcher_digest": self.launcher_digest,
            "version": self.version,
            "identity": self.identity,
        }


def bazel_resolver_identity(toolchain: BazelToolchain) -> ContentIdentity:
    """Bind the Bzlmod resolver semantics to the exact selected Bazel binary."""

    return canonical_identity(
        {
            "resolver_id": BZLMOD_RESOLVER_ID,
            "build_system_toolchain_identity": toolchain.identity,
        }
    )


def _parse_command(raw: str, *, label: str) -> tuple[str, ...]:
    try:
        command = tuple(shlex.split(raw, posix=os.name != "nt"))
    except ValueError as exc:
        raise BuildError("builder.bazel_toolchain", f"{label} is invalid") from exc
    if os.name == "nt" and command:
        executable = command[0]
        if (
            len(executable) >= 2
            and executable[0] == executable[-1]
            and executable[0] in {'"', "'"}
        ):
            command = (executable[1:-1], *command[1:])
    if not command:
        raise BuildError("builder.bazel_toolchain", f"{label} is empty")
    return command


def _probe_bazel(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    cwd: Path | None,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> str:
    completed = _run_bounded_process(
        [*command, "--version"],
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        error_prefix="builder.bazel_version",
    )
    if completed.returncode != 0:
        raise BuildError("builder.bazel_version_failed", "Bazel version probe failed")
    try:
        lines = completed.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise BuildError(
            "builder.bazel_version_failed", "Bazel reported a non-UTF-8 version"
        ) from exc
    if len(lines) != 1 or _BAZEL_VERSION.fullmatch(lines[0]) is None:
        raise BuildError(
            "builder.bazel_version_failed",
            "Bazel did not report one supported semantic version",
        )
    if not lines[0].startswith("bazel 9."):
        raise BuildError(
            "builder.bazel_version_unsupported", "Bazel major version 9 is required"
        )
    return lines[0]


def discover_bazel_toolchain(
    environment: Mapping[str, str] | None = None,
    *,
    pinned_command: str | Sequence[str] | None = None,
    workspace_root: Path | None = None,
    timeout_seconds: float = DEFAULT_BAZEL_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_BAZEL_STDERR_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_BAZEL_STDERR_LIMIT_BYTES,
) -> BazelToolchain:
    """Resolve explicit ``BAZEL`` or a direct SHA-addressed cached Bazel binary."""

    configured = dict(os.environ if environment is None else environment)
    explicit: tuple[str, ...] | None = None
    if pinned_command is not None:
        if isinstance(pinned_command, str):
            explicit = _parse_command(pinned_command, label="pinned Bazel command")
        else:
            explicit = tuple(pinned_command)
            if not explicit or any(
                not isinstance(item, str) or not item for item in explicit
            ):
                raise BuildError(
                    "builder.bazel_toolchain", "pinned Bazel command is invalid"
                )
    elif configured.get("BAZEL", "").strip():
        explicit = _parse_command(configured["BAZEL"], label="BAZEL")
    if explicit is None:
        return _discover_content_addressed_bazel(
            configured,
            workspace_root=workspace_root,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
        )
    candidates = (explicit,)
    last_error: BuildError | None = None
    for candidate in candidates:
        found = shutil.which(candidate[0], path=configured.get("PATH"))
        if found is None:
            if explicit is not None:
                raise BuildError(
                    "builder.bazel_toolchain_unavailable",
                    "Configured Bazel command was not found on PATH",
                )
            continue
        invocation = Path(os.path.abspath(found))
        try:
            launcher = _require_content_addressed_bazel_executable(
                invocation, configured
            )
            launcher_digest = executable_file_digest(launcher)
            command = (str(launcher), *candidate[1:])
            version = _probe_bazel(
                command,
                configured,
                cwd=workspace_root,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
            if executable_file_digest(launcher) != launcher_digest:
                raise BuildError(
                    "builder.bazel_toolchain_changed",
                    "Bazel launcher changed during discovery",
                )
            return BazelToolchain(
                command=command,
                launcher_executable=str(launcher),
                launcher_digest=launcher_digest,
                version=version,
            )
        except (BuildError, OSError) as exc:
            if isinstance(exc, BuildError):
                last_error = exc
            if explicit is not None:
                raise
    error = BuildError(
        "builder.bazel_toolchain_unavailable",
        "no Bazel 9 command was found through BAZEL or PATH",
    )
    if last_error is not None:
        raise error from last_error
    raise error


def _discover_content_addressed_bazel(
    environment: Mapping[str, str],
    *,
    workspace_root: Path | None,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> BazelToolchain:
    """Select a direct Bazel binary from Bazelisk's content-addressed cache.

    PATH launchers are deliberately not executed here: Bazelisk is a dynamic
    downloader/selector, not the binary whose dependency closure the build runs.
    """

    try:
        cache_root = _bazelisk_cache_root(environment).resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.bazel_direct_toolchain_unavailable",
            "no safe content-addressed Bazel cache is available",
        ) from exc
    digest_root = cache_root / "downloads" / "sha256"
    try:
        digest_metadata = digest_root.lstat()
    except OSError as exc:
        raise BuildError(
            "builder.bazel_direct_toolchain_unavailable",
            "no safe content-addressed Bazel cache is available; populate Bazelisk's "
            "cache or configure BAZEL with an exact direct Bazel binary",
        ) from exc
    digest_attributes = getattr(digest_metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(digest_metadata.st_mode)
        or stat.S_ISLNK(digest_metadata.st_mode)
        or digest_attributes & 0x0400
    ):
        raise BuildError(
            "builder.bazel_direct_toolchain_unavailable",
            "no safe content-addressed Bazel cache is available; populate Bazelisk's "
            "cache or configure BAZEL with an exact direct Bazel binary",
        )
    executable_name = "bazel.exe" if os.name == "nt" else "bazel"
    candidates: list[tuple[tuple[int, int, int, str], BazelToolchain]] = []
    try:
        entries = sorted(digest_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise BuildError(
            "builder.bazel_cache_unsafe",
            "Bazel content-addressed cache cannot be enumerated safely",
        ) from exc
    if len(entries) > _MAX_BAZELISK_CACHE_ENTRIES:
        raise BuildError(
            "builder.bazel_cache_ambiguous",
            "Bazelisk content-addressed cache exceeds the inspection limit",
        )
    for entry in entries:
        if _SHA256_HEX.fullmatch(entry.name) is None:
            continue
        try:
            metadata = entry.lstat()
        except OSError as exc:
            raise BuildError(
                "builder.bazel_cache_unsafe", "Bazel cache entry is unavailable"
            ) from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or attributes & 0x0400
        ):
            raise BuildError(
                "builder.bazel_cache_unsafe",
                "Bazel content-addressed cache contains a link or junction",
            )
        executable = entry / "bin" / executable_name
        bin_directory = entry / "bin"
        try:
            bin_metadata = bin_directory.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BuildError(
                "builder.bazel_cache_unsafe", "cached Bazel bin path is unavailable"
            ) from exc
        bin_attributes = getattr(bin_metadata, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(bin_metadata.st_mode)
            or stat.S_ISLNK(bin_metadata.st_mode)
            or bin_attributes & 0x0400
        ):
            raise BuildError(
                "builder.bazel_cache_unsafe",
                "cached Bazel bin path contains a link or junction",
            )
        try:
            executable_metadata = executable.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BuildError(
                "builder.bazel_cache_unsafe", "cached Bazel binary is unavailable"
            ) from exc
        executable_attributes = getattr(executable_metadata, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(executable_metadata.st_mode)
            or stat.S_ISLNK(executable_metadata.st_mode)
            or executable_attributes & 0x0400
        ):
            raise BuildError(
                "builder.bazel_cache_unsafe",
                "cached Bazel binary is not a safe regular file",
            )
        digest = executable_file_digest(executable)
        if digest != f"sha256:{entry.name}":
            raise BuildError(
                "builder.bazel_cache_digest_mismatch",
                "cached Bazel binary does not match its content-addressed directory",
            )
        command = (str(executable),)
        try:
            version = _probe_bazel(
                command,
                environment,
                cwd=workspace_root,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
        except BuildError as exc:
            if exc.code == "builder.bazel_version_unsupported":
                continue
            raise
        if executable_file_digest(executable) != digest:
            raise BuildError(
                "builder.bazel_toolchain_changed",
                "cached Bazel binary changed during direct discovery",
            )
        match = _BAZEL_VERSION.fullmatch(version)
        assert match is not None
        if int(match.group("major")) != 9:
            continue
        candidates.append(
            (
                (
                    int(match.group("major")),
                    int(match.group("minor")),
                    int(match.group("patch")),
                    version,
                ),
                BazelToolchain(
                    command=command,
                    launcher_executable=str(executable),
                    launcher_digest=digest,
                    version=version,
                ),
            )
        )
    if not candidates:
        raise BuildError(
            "builder.bazel_direct_toolchain_unavailable",
            "Bazelisk cache contains no content-addressed direct Bazel 9 binary",
        )
    selected_version = max(version for version, _toolchain in candidates)
    selected = [
        toolchain for version, toolchain in candidates if version == selected_version
    ]
    if len(selected) != 1:
        raise BuildError(
            "builder.bazel_cache_ambiguous",
            "Bazelisk cache contains multiple direct binaries for the selected version",
        )
    return selected[0]


def _bazelisk_cache_root(environment: Mapping[str, str]) -> Path:
    configured = environment.get("BAZELISK_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    home = environment.get("HOME", "").strip()
    if sys.platform == "darwin":
        if not home:
            raise BuildError(
                "builder.bazel_direct_toolchain_unavailable",
                "HOME is required to locate the Bazelisk cache",
            )
        return Path(home) / "Library" / "Caches" / "bazelisk"
    if os.name == "nt":
        local = environment.get("LOCALAPPDATA", "").strip()
        if not local:
            raise BuildError(
                "builder.bazel_direct_toolchain_unavailable",
                "LOCALAPPDATA is required to locate the Bazelisk cache",
            )
        return Path(local) / "bazelisk"
    cache = environment.get("XDG_CACHE_HOME", "").strip()
    if cache:
        return Path(cache) / "bazelisk"
    if not home:
        raise BuildError(
            "builder.bazel_direct_toolchain_unavailable",
            "HOME is required to locate the Bazelisk cache",
        )
    return Path(home) / ".cache" / "bazelisk"


def _require_content_addressed_bazel_executable(
    configured_path: Path, environment: Mapping[str, str]
) -> Path:
    """Prove an explicit Bazel command is the direct SHA-addressed binary."""

    try:
        configured_metadata = configured_path.lstat()
        cache_root = _bazelisk_cache_root(environment).resolve(strict=True)
        executable = configured_path.resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.bazel_direct_toolchain_required",
            "Configured BAZEL must name a direct content-addressed Bazel binary",
        ) from exc
    configured_attributes = getattr(configured_metadata, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(configured_metadata.st_mode)
        or stat.S_ISLNK(configured_metadata.st_mode)
        or configured_attributes & 0x0400
    ):
        raise BuildError(
            "builder.bazel_direct_toolchain_required",
            "Configured BAZEL must not be a launcher, link, or junction",
        )
    digest_root = cache_root / "downloads" / "sha256"
    try:
        relative = executable.relative_to(digest_root)
    except ValueError as exc:
        raise BuildError(
            "builder.bazel_direct_toolchain_required",
            "Configured BAZEL must name a direct binary in the SHA-addressed "
            "Bazelisk cache",
        ) from exc
    executable_name = "bazel.exe" if os.name == "nt" else "bazel"
    if (
        len(relative.parts) != 3
        or _SHA256_HEX.fullmatch(relative.parts[0]) is None
        or relative.parts[1:] != ("bin", executable_name)
    ):
        raise BuildError(
            "builder.bazel_direct_toolchain_required",
            "Configured BAZEL does not have the direct SHA-addressed cache layout",
        )
    entry = digest_root / relative.parts[0]
    for path, expected_type in (
        (digest_root, stat.S_ISDIR),
        (entry, stat.S_ISDIR),
        (entry / "bin", stat.S_ISDIR),
        (executable, stat.S_ISREG),
    ):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise BuildError(
                "builder.bazel_direct_toolchain_required",
                "Configured Bazel cache path is unavailable",
            ) from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            not expected_type(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or attributes & 0x0400
        ):
            raise BuildError(
                "builder.bazel_direct_toolchain_required",
                "Configured Bazel cache path contains a link or junction",
            )
    if executable_file_digest(executable) != f"sha256:{relative.parts[0]}":
        raise BuildError(
            "builder.bazel_cache_digest_mismatch",
            "Configured Bazel binary does not match its content-addressed directory",
        )
    return executable


def _strict_json(content: bytes, *, label: str) -> object:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(
            "builder.bazel_evidence_invalid", f"{label} is not UTF-8"
        ) from exc

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BuildError(
            "builder.bazel_evidence_invalid", f"{label} is not strict JSON"
        ) from exc


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildError("builder.bazel_evidence_invalid", f"{label} is empty")
    return value


def _node_children(node: Mapping[str, object], field_name: str) -> list[object]:
    value = node.get(field_name)
    if not isinstance(value, list):
        raise BuildError(
            "builder.bazel_graph_invalid",
            f"expanded Bazel graph node lacks {field_name}",
        )
    return value


def _normalized_module_graph(
    content: bytes,
) -> tuple[bytes, BzlmodRootModule, tuple[BzlmodModule, ...]]:
    document = _strict_json(content, label="Bazel module graph")
    if not isinstance(document, Mapping):
        raise BuildError(
            "builder.bazel_graph_invalid", "Bazel graph root is not an object"
        )
    queue: list[tuple[Mapping[str, object], bool]] = [(document, True)]
    definitions: dict[str, BzlmodModule] = {}
    metadata: dict[str, tuple[str, str]] = {}
    referenced: set[str] = set()
    root_module: BzlmodRootModule | None = None
    visited_nodes = 0
    while queue:
        node, is_root = queue.pop()
        visited_nodes += 1
        if visited_nodes > 100_000:
            raise BuildError("builder.bazel_graph_invalid", "Bazel graph is too large")
        unknown = set(node) - _GRAPH_NODE_FIELDS
        if unknown:
            raise BuildError(
                "builder.bazel_graph_invalid",
                "Bazel graph uses unsupported node fields: "
                + ", ".join(sorted(unknown)),
            )
        key = _text(node.get("key"), label="Bazel module key")
        name = _text(node.get("name"), label="Bazel module name")
        version = _text(node.get("version"), label="Bazel module version")
        _text(node.get("apparentName"), label="Bazel apparent module name")
        unexpanded = node.get("unexpanded", False)
        if type(unexpanded) is not bool:
            raise BuildError(
                "builder.bazel_graph_invalid", "Bazel unexpanded flag is invalid"
            )
        if is_root:
            if key != "<root>" or node.get("root") is not True or unexpanded:
                raise BuildError(
                    "builder.bazel_graph_invalid", "Bazel graph root is invalid"
                )
        elif key != f"{name}@{version}" or key == "<root>":
            raise BuildError(
                "builder.bazel_graph_invalid",
                "Bazel module key does not bind its exact name and version",
            )
        prior = metadata.setdefault(key, (name, version))
        if prior != (name, version):
            raise BuildError(
                "builder.bazel_graph_invalid", "Bazel module metadata conflicts"
            )
        if unexpanded:
            if set(node) != {"key", "name", "version", "apparentName", "unexpanded"}:
                raise BuildError(
                    "builder.bazel_graph_invalid",
                    "unexpanded Bazel graph reference carries unexpected fields",
                )
            referenced.add(key)
            continue
        dependencies = _node_children(node, "dependencies")
        indirect = _node_children(node, "indirectDependencies")
        cycles = _node_children(node, "cycles")
        if indirect:
            raise BuildError(
                "builder.bazel_graph_indirect_unsupported",
                "full Bazel graph unexpectedly contains display-only indirect edges",
            )
        children = [*dependencies, *cycles]
        if any(not isinstance(child, Mapping) for child in children):
            raise BuildError(
                "builder.bazel_graph_invalid", "Bazel graph child is invalid"
            )
        child_keys = tuple(
            sorted(
                {
                    _text(child.get("key"), label="Bazel dependency key")
                    for child in children
                }
            )
        )
        referenced.update(child_keys)
        queue.extend((child, False) for child in children if isinstance(child, Mapping))
        if is_root:
            root_module = BzlmodRootModule(name, version, child_keys)
        else:
            module = BzlmodModule(key, name, version, child_keys)
            existing = definitions.get(key)
            if existing is not None and existing != module:
                raise BuildError(
                    "builder.bazel_graph_invalid", "Bazel module definitions conflict"
                )
            definitions[key] = module
    if root_module is None:
        raise BuildError("builder.bazel_graph_invalid", "Bazel graph omits its root")
    missing = sorted(referenced - definitions.keys())
    if missing:
        raise BuildError(
            "builder.bazel_graph_incomplete",
            "Bazel graph has unexpanded modules without definitions: "
            + ", ".join(missing),
        )
    modules = tuple(definitions[key] for key in sorted(definitions))
    normalized = _canonical_json(
        {
            "schema": BuildDependencyObservation.GRAPH_SCHEMA,
            "root_module": root_module.to_dict(),
            "modules": [module.to_dict() for module in modules],
        }
    )
    return normalized, root_module, modules


def _normalize_repository_value(value: object) -> object:
    if isinstance(value, Mapping):
        normalized = {
            str(key): _normalize_repository_value(item) for key, item in value.items()
        }
        for field_name in (
            "stringDictValue",
            "stringListDictValue",
            "labelDictUnaryValue",
            "labelKeyedStringDictValue",
        ):
            entries = normalized.get(field_name)
            if isinstance(entries, list) and all(
                isinstance(item, Mapping) and isinstance(item.get("key"), str)
                for item in entries
            ):
                normalized[field_name] = sorted(
                    entries, key=lambda item: str(item["key"])
                )
        return normalized
    if isinstance(value, list):
        return [_normalize_repository_value(item) for item in value]
    return value


def _attribute(record: Mapping[str, object], name: str) -> Mapping[str, object] | None:
    attributes = record.get("attribute")
    if not isinstance(attributes, list):
        return None
    return next(
        (
            item
            for item in attributes
            if isinstance(item, Mapping) and item.get("name") == name
        ),
        None,
    )


def _attribute_text(record: Mapping[str, object], name: str) -> str:
    attribute = _attribute(record, name)
    if attribute is None:
        return ""
    value = attribute.get("stringValue")
    return value if isinstance(value, str) else ""


def _attribute_strings(record: Mapping[str, object], name: str) -> tuple[str, ...]:
    attribute = _attribute(record, name)
    if attribute is None:
        return ()
    scalar = attribute.get("stringValue")
    if isinstance(scalar, str) and scalar:
        return (scalar,)
    sequence = attribute.get("stringListValue")
    if isinstance(sequence, list) and all(
        isinstance(item, str) and item for item in sequence
    ):
        return tuple(sequence)
    return ()


def _normalized_repositories(
    content: bytes, *, modules: tuple[BzlmodModule, ...]
) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(
            "builder.bazel_repositories_invalid",
            "Bazel repository definitions are not UTF-8",
        ) from exc
    records: dict[str, dict[str, object]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        raw = _strict_json(
            line.encode("utf-8"), label=f"Bazel repository line {line_number}"
        )
        if not isinstance(raw, Mapping) or set(raw) - _REPOSITORY_FIELDS:
            raise BuildError(
                "builder.bazel_repositories_invalid",
                "Bazel repository definition has unsupported fields",
            )
        canonical_name = _text(
            raw.get("canonicalName"), label="canonical repository name"
        )
        _text(raw.get("repoRuleName"), label="repository rule name")
        _text(raw.get("repoRuleBzlLabel"), label="repository rule label")
        attributes = raw.get("attribute", [])
        if not isinstance(attributes, list) or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("type"), str)
            for item in attributes
        ):
            raise BuildError(
                "builder.bazel_repositories_invalid",
                "Bazel repository attributes are invalid",
            )
        normalized = dict(_normalize_repository_value(raw))
        normalized["attribute"] = attributes
        normalized["attribute"] = sorted(
            normalized["attribute"], key=lambda item: str(item["name"])
        )
        if canonical_name in records:
            raise BuildError(
                "builder.bazel_repositories_invalid",
                f"duplicate Bazel repository {canonical_name}",
            )
        records[canonical_name] = normalized
    names = [module.name for module in modules]
    if len(names) != len(set(names)):
        raise BuildError(
            "builder.bazel_multiversion_unsupported",
            "Bazel conformance currently requires one selected version per module name",
        )
    missing: list[str] = []
    for module in modules:
        canonical_name = (
            module.name
            if module.name in _WELL_KNOWN_UNVERSIONED_MODULE_REPOSITORIES
            else f"{module.name}+"
        )
        record = records.get(canonical_name)
        if record is None:
            missing.append(module.key)
            continue
        if record.get("repoRuleName") != "http_archive":
            raise BuildError(
                "builder.bazel_repository_unsupported",
                f"registry module {module.key} is not backed by http_archive",
            )
        urls = _attribute_strings(record, "urls")
        module_urls = _attribute_strings(record, "remote_module_file_urls")
        bcr_module_url = (
            f"https://bcr.bazel.build/modules/{module.name}/{module.version}/"
            "MODULE.bazel"
        )
        if (
            not urls
            or any(
                not (url.startswith("https://") or url.startswith("http://"))
                for url in urls
            )
            or not module_urls
            or bcr_module_url not in module_urls
            or not (
                _attribute_text(record, "integrity")
                or _attribute_text(record, "sha256")
            )
            or not _attribute_text(record, "remote_module_file_integrity")
        ):
            raise BuildError(
                "builder.bazel_repository_unpinned",
                f"registry module {module.key} lacks exact remote content evidence",
            )
    if missing:
        raise BuildError(
            "builder.bazel_repositories_incomplete",
            "Bazel repository evidence does not cover modules: " + ", ".join(missing),
        )
    return b"".join(_canonical_json(records[name]) + b"\n" for name in sorted(records))


def _request(value: Mapping[str, object]) -> BuildRequest:
    fields = {
        key: value[key]
        for key in (
            "effective_revision_digest",
            "source_bundle_digest",
            "builder_id",
            "toolchain_digest",
            "sandbox_profile",
            "requested_privileges",
            "allowed_outputs",
        )
    }
    if "schema" in value:
        fields["schema"] = value["schema"]
    return BuildRequest.from_dict(fields)


def _authorization(value: Mapping[str, object]) -> BuildAuthorization:
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


def _artifact_files(request: Mapping[str, object]) -> dict[str, bytes]:
    artifact = request.get("artifact")
    if not isinstance(artifact, Mapping):
        raise BuildError(
            "builder.bazel_artifact_missing",
            "Bazel decorator requires generated source",
        )
    raw_files = artifact.get("files")
    if not isinstance(raw_files, Mapping) or not raw_files:
        raise BuildError(
            "builder.bazel_artifact_missing", "generated source has no files"
        )
    result: dict[str, bytes] = {}
    for raw_path, raw_content in raw_files.items():
        if not isinstance(raw_path, str) or not isinstance(raw_content, str):
            raise BuildError(
                "builder.bazel_artifact_invalid", "generated source files must be text"
            )
        path = canonical_relative_posix_path(
            raw_path, label="generated Bazel source path"
        )
        result[path.as_posix()] = raw_content.encode("utf-8")
    return result


def _materialize(root: Path, files: Mapping[str, bytes]) -> None:
    for relative, content in sorted(files.items()):
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _build_input_consumption(
    buildfiles_content: bytes,
    source_inputs_content: bytes,
    *,
    projection: Path,
    workspace: Path,
    generated_files: Mapping[str, bytes],
    source_bundle_digest: str,
) -> BuildInputConsumption:
    """Bind Bazel's local ``buildfiles(//...)`` result to generated-tree paths."""

    workspace_relative = workspace.relative_to(projection)
    consumed = {(workspace_relative / "MODULE.bazel").as_posix()}
    for evidence_name, content in (
        ("build-file", buildfiles_content),
        ("target-source", source_inputs_content),
    ):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuildError(
                "builder.bazel_build_inputs_invalid",
                f"Bazel {evidence_name} labels are not UTF-8",
            ) from exc
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            label = raw_line.strip()
            if not label:
                continue
            if label.startswith("@"):
                continue
            if not label.startswith("//") or label.count(":") != 1:
                raise BuildError(
                    "builder.bazel_build_inputs_invalid",
                    f"Bazel {evidence_name} line {line_number} is not an exact label",
                )
            package, name = label[2:].split(":", 1)
            raw_relative = f"{package}/{name}" if package else name
            relative = canonical_relative_posix_path(
                raw_relative, label=f"Bazel local {evidence_name} input"
            )
            consumed.add((workspace_relative / relative).as_posix())

    bazelrc = (workspace_relative / ".bazelrc").as_posix()
    if bazelrc in generated_files:
        consumed.add(bazelrc)
    unknown = consumed.difference(generated_files)
    if unknown:
        raise BuildError(
            "builder.bazel_build_inputs_invalid",
            "Bazel named build inputs outside the generated tree: "
            + ", ".join(sorted(unknown)[:5]),
        )
    return BuildInputConsumption(
        consumer_id=BZLMOD_RESOLVER_ID,
        source_bundle_digest=source_bundle_digest,
        files=tuple(sorted(consumed)),
    )


def _copy_delegate_artifact(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise BuildError(
            "builder.bazel_delegate_artifact_invalid",
            "delegate artifact is not a directory",
        )
    canonical_tree_digest(source)
    for child in source.iterdir():
        if child.name == ".literate":
            raise BuildError(
                "builder.bazel_artifact_collision",
                "delegate owns the reserved .literate metadata path",
            )
        target = (
            destination / ".literate" / "delegate" / child.name
            if child.name == "build-manifest.json"
            else destination / child.name
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if child.is_dir():
            shutil.copytree(child, target)
        elif child.is_file():
            shutil.copy2(child, target)
        else:
            raise BuildError(
                "builder.bazel_delegate_artifact_invalid",
                "delegate artifact contains a special file",
            )


def _file_records(
    root: Path, *, excluded: frozenset[str] = frozenset()
) -> list[dict[str, str]]:
    files: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BuildError(
                "builder.bazel_artifact_invalid",
                "composite artifact contains a symlink",
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise BuildError(
                "builder.bazel_artifact_invalid",
                "composite artifact contains a special file",
            )
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        files.append((relative, path))
    records: list[dict[str, str]] = []
    # Path ordering is host-flavored: Windows compares case-insensitively. Wire
    # authority is canonical UTF-8 text, so sort the portable path strings instead.
    for relative, path in sorted(files, key=lambda item: item[0]):
        content = path.read_bytes()
        records.append({"path": relative, "digest": _sha256(content)})
    return records


def _cached_composite_matches(root: Path, manifest_bytes: bytes) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    manifest_path = root / "build-manifest.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.read_bytes() != manifest_bytes
    ):
        return False
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError:
        return False
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), list):
        return False
    return manifest["files"] == _file_records(
        root, excluded=frozenset({"build-manifest.json"})
    )


class BazelConformanceBuildAdapter:
    """Decorate an authorized native build with exact Bazel conformance evidence.

    ``build_composite`` is the standard typed path. ``build`` remains an explicit
    compatibility reader for version-1 lifecycle callers.
    """

    resolver_id = BZLMOD_RESOLVER_ID

    def __init__(
        self,
        delegate: _DelegateBuilder | CompositeBuildDelegate,
        bazel_toolchain: BazelToolchain,
        artifact_store: Path,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        timeout_seconds: float = DEFAULT_BAZEL_COMMAND_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_BAZEL_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_BAZEL_STDERR_LIMIT_BYTES,
        clock: Callable[[], datetime] | None = None,
        cache_root: Path | None = None,
        build_options: Sequence[str] = (),
    ) -> None:
        if not isinstance(getattr(delegate, "builder_id", None), str):
            raise ValueError("Bazel conformance delegate must expose builder_id")
        if timeout_seconds <= 0 or stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("Bazel command bounds must be positive")
        self.delegate = delegate
        self.bazel_toolchain = bazel_toolchain
        self.artifact_store = Path(artifact_store)
        self.authorization_verifier = (
            authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self.timeout_seconds = timeout_seconds
        self.stdout_limit_bytes = stdout_limit_bytes
        self.stderr_limit_bytes = stderr_limit_bytes
        self.clock = clock or (lambda: datetime.now(UTC))
        self.cache_root = None if cache_root is None else Path(cache_root).resolve()
        if any(
            not isinstance(option, str) or not option.startswith("--")
            for option in build_options
        ):
            raise ValueError("Bazel build options must be command-line options")
        self.build_options = tuple(build_options)

    @property
    def builder_id(self) -> str:
        return self.delegate.builder_id

    @property
    def resolver_identity(self) -> ContentIdentity:
        return bazel_resolver_identity(self.bazel_toolchain)

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Read the legacy build wire until all v1 lifecycle callers migrate."""

        return self._build(request, authorization, composite=None)

    def build_composite(
        self,
        composite_request: CompositeBuildRequest,
        manifest: ComponentBuildManifest,
        materialization_plan: ArtifactMaterializationPlan,
        request: BuildRequest,
        authorization: BuildAuthorization,
        artifact: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Consume and revalidate the complete typed build authority."""

        if not isinstance(self.delegate, CompositeBuildDelegate):
            raise BuildError(
                "builder.composite_delegate_required",
                "typed Bazel builds require a composite-aware native delegate",
            )
        selected_build_toolchain = ContentIdentity.parse_uri(
            self.bazel_toolchain.identity
        )
        if (
            self.delegate.build_system_resolver_identity != self.resolver_identity
            or self.delegate.build_system_toolchain_identity != selected_build_toolchain
        ):
            raise BuildError(
                "builder.composite_request_mismatch",
                "native delegate is bound to another build-system authority",
            )
        validate_composite_build_invocation(
            composite_request,
            manifest,
            materialization_plan,
            request,
            authorization,
            artifact,
            build_system_resolver_identity=self.resolver_identity,
            build_system_toolchain_identity=selected_build_toolchain,
            language_runtime_identity=self.delegate.language_runtime_identity,
        )
        request_document = {**request.to_dict(), "artifact": artifact}
        return self._build(
            request_document,
            authorization.to_dict(),
            composite=_CompositeInvocation(
                request=composite_request,
                manifest=manifest,
                materialization_plan=materialization_plan,
                artifact=artifact,
            ),
        )

    def _build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
        *,
        composite: _CompositeInvocation | None,
    ) -> Mapping[str, object]:
        typed_request = _request(request)
        typed_authorization = _authorization(authorization)
        self.authorization_verifier.require_build_valid(
            typed_authorization,
            typed_request,
            now=self.clock(),
        )
        require_unsandboxed_host_build_authorization(typed_request, typed_authorization)
        if typed_request.builder_id != self.builder_id:
            raise BuildError(
                "builder.identity_mismatch", "build request selected another delegate"
            )
        if BAZEL_DEPENDENCY_EVIDENCE_OUTPUT not in typed_request.allowed_outputs:
            raise BuildError(
                "builder.bazel_evidence_not_authorized",
                "Bazel dependency evidence output is not authorized",
            )
        files = _artifact_files(request)
        environment = dict(os.environ)
        with _temporary_bazel_directory() as directory:
            work = Path(directory)
            projection = work / "source-projection"
            projection.mkdir()
            _materialize(projection, files)
            source_digest = canonical_tree_digest(projection)
            if source_digest != typed_request.source_bundle_digest:
                raise BuildError(
                    "builder.source_digest_mismatch",
                    "generated source changed after Bazel build authorization",
                )
            modules = tuple(projection.rglob("MODULE.bazel"))
            if len(modules) != 1 or modules[0].is_symlink() or not modules[0].is_file():
                raise BuildError(
                    "builder.bazel_module_missing",
                    "Bazel conformance requires exactly one generated MODULE.bazel",
                )
            workspace = modules[0].parent
            output_base = work / "output-base"
            links = work / "links"
            startup = [
                *self.bazel_toolchain.command,
                # A per-invocation batch JVM avoids a persistent Bazel server retaining
                # output-base files on Windows after either success or failure.
                "--batch",
                "--nosystem_rc",
                "--nohome_rc",
                "--noworkspace_rc",
                f"--output_base={output_base}",
            ]
            project_rc = workspace / ".bazelrc"
            if project_rc.exists():
                if project_rc.is_symlink() or not project_rc.is_file():
                    raise BuildError(
                        "builder.bazel_rc_invalid", "generated .bazelrc is invalid"
                    )
                startup.insert(
                    len(self.bazel_toolchain.command) + 3, f"--bazelrc={project_rc}"
                )

            def run(phase: str, arguments: Sequence[str]) -> BoundedProcessResult:
                if not arguments:
                    raise ValueError("Bazel invocation requires a command")
                persistent_options: list[str] = []
                if self.cache_root is not None:
                    repository_cache = self.cache_root / "repository-cache"
                    disk_cache = self.cache_root / "disk-cache"
                    repository_cache.mkdir(parents=True, exist_ok=True)
                    disk_cache.mkdir(parents=True, exist_ok=True)
                    persistent_options.append(f"--repository_cache={repository_cache}")
                    if arguments[0] == "build":
                        persistent_options.append(f"--disk_cache={disk_cache}")
                command = [
                    *startup,
                    arguments[0],
                    *persistent_options,
                    *self.build_options,
                    *arguments[1:],
                ]
                self.bazel_toolchain.require_unchanged(environment, cwd=workspace)
                try:
                    completed = _run_bounded_process(
                        command,
                        cwd=workspace,
                        environment=environment,
                        timeout_seconds=self.timeout_seconds,
                        stdout_limit_bytes=self.stdout_limit_bytes,
                        stderr_limit_bytes=self.stderr_limit_bytes,
                        error_prefix=f"builder.bazel_{phase}",
                    )
                finally:
                    self.bazel_toolchain.require_unchanged(environment, cwd=workspace)
                if completed.returncode != 0:
                    detail = (completed.stdout + completed.stderr)[-4000:].decode(
                        "utf-8", errors="replace"
                    )
                    raise BuildError(
                        f"builder.bazel_{phase}_failed",
                        f"Bazel {phase} failed: {detail}",
                    )
                return completed

            dependency_evidence = collect_bzlmod_dependency_evidence(
                run=run,
                workspace=workspace,
                source_bundle_digest=typed_request.source_bundle_digest,
                build_toolchain_identity=typed_request.toolchain_digest,
                resolver_toolchain_identity=self.bazel_toolchain.identity,
            )
            lock_path = workspace / "MODULE.bazel.lock"
            lock_bytes = dependency_evidence.lock_bytes
            frozen_projection_digest = canonical_tree_digest(projection)
            build_input_evidence = collect_bazel_build_input_evidence(
                run=run,
                projection=projection,
                workspace=workspace,
                generated_files=files,
                source_bundle_digest=typed_request.source_bundle_digest,
            )
            build_input_consumption = build_input_evidence.consumption

            def build_delegate() -> tuple[dict[str, object], Path, str]:
                if composite is not None:
                    if not isinstance(self.delegate, CompositeBuildDelegate):
                        raise BuildError(
                            "builder.composite_delegate_required",
                            "typed Bazel builds require a composite-aware "
                            "native delegate",
                        )
                    result = dict(
                        self.delegate.build_composite(
                            composite.request,
                            composite.manifest,
                            composite.materialization_plan,
                            typed_request,
                            typed_authorization,
                            composite.artifact,
                            build_input_consumption,
                        )
                    )
                elif isinstance(self.delegate, BuildInputConsumptionAwareBuilder):
                    result = dict(
                        self.delegate.build_with_input_consumption(
                            request,
                            authorization,
                            build_input_consumption,
                        )
                    )
                else:
                    result = dict(self.delegate.build(request, authorization))
                require_build_result(
                    result,
                    source_bundle_digest=typed_request.source_bundle_digest,
                    authorization_id=typed_authorization.authorization_id,
                    toolchain_identity=typed_request.toolchain_digest,
                )
                path_raw = result.get("artifact_path")
                if not isinstance(path_raw, (str, os.PathLike)):
                    raise BuildError(
                        "builder.bazel_delegate_artifact_invalid",
                        "delegate omitted artifact_path",
                    )
                path = Path(path_raw).resolve(strict=True)
                digest = result.get("artifact_digest")
                if not isinstance(digest, str):
                    raise BuildError(
                        "builder.bazel_delegate_artifact_invalid",
                        "delegate omitted artifact_digest",
                    )
                return result, path, digest

            try:
                run(
                    "build",
                    (
                        "build",
                        "--lockfile_mode=error",
                        f"--symlink_prefix={links}/",
                        "//...",
                    ),
                )
            except BuildError as bazel_failure:
                if bazel_failure.code != "builder.bazel_build_failed":
                    raise
                # A phase-level Bazel nonzero is not source-defect evidence. Ask the
                # exact authorized native builder to discriminate. Only its stronger,
                # language-specific typed result can later authorize replacement.
                try:
                    build_delegate()
                except BuildError as delegate_failure:
                    raise delegate_failure from bazel_failure
                raise
            if lock_path.read_bytes() != lock_bytes:
                raise BuildError(
                    "builder.bazel_lock_changed", "frozen MODULE.bazel.lock changed"
                )
            if canonical_tree_digest(projection) != frozen_projection_digest:
                raise BuildError(
                    "builder.bazel_projection_changed",
                    "Bazel changed frozen generated source",
                )
            delegate_result, delegate_path, delegate_digest = build_delegate()
        self.artifact_store.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix="bazel-conformance-", dir=self.artifact_store)
        )
        try:
            _copy_delegate_artifact(delegate_path, staging)
            evidence_dir = staging / ".literate" / "bazel"
            if evidence_dir.exists():
                raise BuildError(
                    "builder.bazel_artifact_collision",
                    "delegate owns reserved Bazel evidence path",
                )
            evidence_dir.mkdir(parents=True)
            buildfiles_path = evidence_dir / "buildfiles.txt"
            buildfiles_path.write_bytes(build_input_evidence.buildfiles_bytes)
            source_inputs_path = evidence_dir / "source-inputs.txt"
            source_inputs_path.write_bytes(build_input_evidence.source_inputs_bytes)
            consumption_path = evidence_dir / "build-input-consumption.json"
            consumption_path.write_bytes(
                _canonical_json(build_input_consumption.to_dict())
            )
            evidence_contents = (
                (
                    "bazel-module-lock",
                    ".literate/bazel/MODULE.bazel.lock",
                    dependency_evidence.lock_bytes,
                ),
                (
                    "bazel-module-graph",
                    ".literate/bazel/module-graph.json",
                    dependency_evidence.graph_bytes,
                ),
                (
                    "bazel-repository-definitions",
                    ".literate/bazel/repositories.ndjson",
                    dependency_evidence.repository_bytes,
                ),
            )
            for _, relative, content in evidence_contents:
                staging.joinpath(*relative.split("/")).write_bytes(content)
            observation = dependency_evidence.observation
            manifest = {
                "schema": "urn:literate-ai:schema:v1:bazel-conformance-artifact",
                "authorization_id": typed_authorization.authorization_id,
                "builder_id": self.builder_id,
                "build_toolchain_identity": typed_request.toolchain_digest,
                "delegate_artifact_digest": delegate_digest,
                "evidence_identity": observation.evidence_identity,
                "graph_identity": observation.graph_identity,
                "observation_identity": observation.observation_identity,
                "build_input_consumption": build_input_consumption.to_dict(),
                "build_input_consumption_identity": build_input_consumption.identity,
                "resolver_toolchain_identity": self.bazel_toolchain.identity,
                "source_bundle_digest": typed_request.source_bundle_digest,
                "files": _file_records(staging),
            }
            manifest_bytes = _canonical_json(manifest)
            artifact_digest = _sha256(manifest_bytes)
            (staging / "build-manifest.json").write_bytes(manifest_bytes)
            if not _cached_composite_matches(staging, manifest_bytes):
                raise BuildError(
                    "builder.bazel_artifact_invalid",
                    "composite artifact differs from manifest",
                )
            final = self.artifact_store / artifact_digest.removeprefix("sha256:")
            if final.exists():
                if not _cached_composite_matches(final, manifest_bytes):
                    raise BuildError(
                        "builder.bazel_artifact_collision",
                        "existing composite artifact differs",
                    )
                shutil.rmtree(staging)
            else:
                os.replace(staging, final)
            return {
                **delegate_result,
                "artifact_digest": artifact_digest,
                "artifact_path": str(final),
                "delegate_artifact_digest": delegate_digest,
                "toolchain_identity": typed_request.toolchain_digest,
                "resolver_toolchain_identity": self.bazel_toolchain.identity,
                "dependency_observation": observation.to_dict(),
                "build_input_consumption": build_input_consumption.to_dict(),
                "build_input_consumption_identity": build_input_consumption.identity,
            }
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise


__all__ = [
    "BAZEL_DEPENDENCY_EVIDENCE_OUTPUT",
    "BZLMOD_RESOLVER_ID",
    "BazelBuildInputEvidence",
    "BazelConformanceBuildAdapter",
    "BazelDependencyEvidence",
    "BazelToolchain",
    "bazel_resolver_identity",
    "collect_bazel_build_input_evidence",
    "collect_bzlmod_dependency_evidence",
    "discover_bazel_toolchain",
]
