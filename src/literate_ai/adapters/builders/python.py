"""Authorization-gated Python bytecode builder used by conformance tests."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
    SecurityProfile,
)

UNSANDBOXED_HOST_BUILD_PROFILE = "explicit-unsandboxed-host-build@1"
UNSANDBOXED_HOST_BUILD_PRIVILEGES = (
    "compiler",
    "devices",
    "host-filesystem",
    "network",
    "package-manager",
    "processes",
    "sandbox-escape",
    "secrets",
)
_SHA256_PREFIX = "sha256:"
_FILE_DIGEST_CHUNK_BYTES = 1024 * 1024
DEFAULT_PYTHON_VERSION_TIMEOUT_SECONDS = 15.0
DEFAULT_PYTHON_BUILD_TIMEOUT_SECONDS = 60.0
DEFAULT_PYTHON_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_PYTHON_STDERR_LIMIT_BYTES = 1024 * 1024
_PYTHON_PROBE_PREFIX = "literate-ai-python-toolchain-v1:"
_PYTHON_PROBE_SCRIPT = (
    "import json,sys;"
    "print('literate-ai-python-toolchain-v1:' + json.dumps({"
    "'cache_tag':sys.implementation.cache_tag,"
    "'executable':sys.executable,"
    "'implementation':sys.implementation.name,"
    "'version':sys.version,"
    "'version_info':list(sys.version_info)"
    "},sort_keys=True,separators=(',',':')))"
)
_PYTHON_COMPILE_SCRIPT = (
    "import py_compile,sys;"
    "py_compile.compile(sys.argv[1],cfile=sys.argv[2],dfile=sys.argv[3],"
    "doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)"
)


class BuildError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
        raise ValueError(f"{label} must be a sha256 digest")
    encoded = value.removeprefix(_SHA256_PREFIX)
    if len(encoded) != 64 or encoded != encoded.casefold():
        raise ValueError(f"{label} must be a sha256 digest")
    try:
        int(encoded, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a sha256 digest") from exc


def executable_file_digest(path: Path) -> str:
    """Hash an exact executable without retaining its potentially large contents."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_FILE_DIGEST_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise BuildError(
            "builder.toolchain_unavailable",
            f"Toolchain executable is unavailable: {path}",
        ) from exc
    return f"sha256:{digest.hexdigest()}"


def _run_bounded_process(*args: object, **kwargs: object):
    # Imported lazily because the shared process helper imports BuildError from this
    # module. At invocation time this module is fully initialized.
    from ._process import run_bounded_process

    return run_bounded_process(*args, **kwargs)


def controlled_python_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Remove ambient Python startup/import hooks from toolchain subprocesses."""

    return {
        key: value
        for key, value in environment.items()
        if not key.casefold().startswith("python")
    }


def _python_toolchain_identity(
    *,
    command: tuple[str, ...],
    launcher_executable: str,
    launcher_digest: str,
    runtime_executable: str,
    runtime_digest: str,
    implementation: str,
    version: str,
    version_info: tuple[int, int, int, str, int],
    cache_tag: str,
) -> str:
    encoded = json.dumps(
        {
            "cache_tag": cache_tag,
            "command": list(command),
            "implementation": implementation,
            "launcher_digest": launcher_digest,
            "launcher_executable": launcher_executable,
            "runtime_digest": runtime_digest,
            "runtime_executable": runtime_executable,
            "version": version,
            "version_info": list(version_info),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class PythonToolchain:
    """One Python command with exact invocation, launcher, runtime, and version.

    The invocation path is retained so virtual environments and shims preserve their
    semantics. Its resolved launcher bytes and the runtime executable reported by the
    interpreter are both bound. They are checked around subprocesses; this detects
    temporal drift but is not an atomic operating-system execution lock.
    """

    command: tuple[str, ...]
    launcher_executable: str
    launcher_digest: str
    runtime_executable: str
    runtime_digest: str
    implementation: str
    version: str
    version_info: tuple[int, int, int, str, int]
    cache_tag: str
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
            or not all(
                isinstance(value, str) and value
                for value in (
                    self.launcher_executable,
                    self.runtime_executable,
                    self.implementation,
                    self.version,
                    self.cache_tag,
                )
            )
        ):
            raise ValueError("Python toolchain fields cannot be empty")
        if (
            not isinstance(self.version_info, tuple)
            or len(self.version_info) != 5
            or any(type(item) is not int for item in self.version_info[:3])
            or not isinstance(self.version_info[3], str)
            or type(self.version_info[4]) is not int
            or self.version_info[0] != 3
        ):
            raise ValueError("Python toolchain requires a Python 3 version tuple")
        _require_digest(self.launcher_digest, "Python launcher identity")
        _require_digest(self.runtime_digest, "Python runtime identity")
        object.__setattr__(
            self,
            "identity",
            _python_toolchain_identity(
                command=self.command,
                launcher_executable=self.launcher_executable,
                launcher_digest=self.launcher_digest,
                runtime_executable=self.runtime_executable,
                runtime_digest=self.runtime_digest,
                implementation=self.implementation,
                version=self.version,
                version_info=self.version_info,
                cache_tag=self.cache_tag,
            ),
        )

    @property
    def executable(self) -> str:
        """Compatibility alias for the exact invocation path."""

        return self.command[0]

    @property
    def executable_digest(self) -> str:
        """Compatibility alias for the resolved launcher byte identity."""

        return self.launcher_digest

    def _require_launcher_unchanged(self) -> None:
        try:
            launcher = Path(self.command[0]).resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.python_toolchain_changed",
                "Python launcher became unavailable after toolchain selection",
            ) from exc
        if str(launcher) != self.launcher_executable or not launcher.is_file():
            raise BuildError(
                "builder.python_toolchain_changed",
                "Python launcher path changed after toolchain selection",
            )
        if executable_file_digest(launcher) != self.launcher_digest:
            raise BuildError(
                "builder.python_toolchain_changed",
                "Python launcher bytes changed after toolchain selection",
            )

    def require_unchanged(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        timeout_seconds: float = DEFAULT_PYTHON_VERSION_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_PYTHON_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_PYTHON_STDERR_LIMIT_BYTES,
    ) -> None:
        configured = dict(os.environ if environment is None else environment)
        self._require_launcher_unchanged()
        try:
            probe = _probe_python_command(
                self.command,
                configured,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
        except BuildError as exc:
            raise BuildError(
                "builder.python_toolchain_changed",
                "Python runtime could not be revalidated after toolchain selection",
            ) from exc
        self._require_launcher_unchanged()
        current = (
            probe.runtime_executable,
            probe.runtime_digest,
            probe.implementation,
            probe.version,
            probe.version_info,
            probe.cache_tag,
        )
        expected = (
            self.runtime_executable,
            self.runtime_digest,
            self.implementation,
            self.version,
            self.version_info,
            self.cache_tag,
        )
        if current != expected:
            raise BuildError(
                "builder.python_toolchain_changed",
                "Python runtime identity changed after toolchain selection",
            )


@dataclass(frozen=True, slots=True)
class _PythonProbe:
    runtime_executable: str
    runtime_digest: str
    implementation: str
    version: str
    version_info: tuple[int, int, int, str, int]
    cache_tag: str


def _probe_python_command(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> _PythonProbe:
    completed = _run_bounded_process(
        [*command, "-I", "-S", "-c", _PYTHON_PROBE_SCRIPT],
        cwd=None,
        environment=controlled_python_environment(environment),
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        error_prefix="builder.python_version",
    )
    if completed.returncode != 0 or completed.stderr:
        raise BuildError(
            "builder.python_version_failed",
            "Python candidate did not complete the isolated version probe",
        )
    try:
        lines = completed.stdout.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise BuildError(
            "builder.python_version_failed",
            "Python candidate reported a non-UTF-8 version",
        ) from exc
    if len(lines) != 1 or not lines[0].startswith(_PYTHON_PROBE_PREFIX):
        raise BuildError(
            "builder.python_version_failed",
            "Python candidate did not report the expected probe record",
        )
    try:
        raw = json.loads(lines[0].removeprefix(_PYTHON_PROBE_PREFIX))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise BuildError(
            "builder.python_version_failed",
            "Python candidate reported an invalid probe record",
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {
        "cache_tag",
        "executable",
        "implementation",
        "version",
        "version_info",
    }:
        raise BuildError(
            "builder.python_version_failed",
            "Python candidate reported an incomplete probe record",
        )
    raw_version = raw["version_info"]
    if (
        not isinstance(raw_version, list)
        or len(raw_version) != 5
        or any(type(item) is not int for item in raw_version[:3])
        or not isinstance(raw_version[3], str)
        or type(raw_version[4]) is not int
        or raw_version[0] != 3
    ):
        raise BuildError(
            "builder.python_version_unsupported",
            "Python candidate is not a Python 3 runtime",
        )
    string_fields = (
        raw["cache_tag"],
        raw["executable"],
        raw["implementation"],
        raw["version"],
    )
    if any(not isinstance(value, str) or not value for value in string_fields):
        raise BuildError(
            "builder.python_version_failed",
            "Python candidate reported empty runtime identity fields",
        )
    try:
        runtime = Path(raw["executable"]).resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.python_toolchain_unavailable",
            "Python candidate's runtime executable is unavailable",
        ) from exc
    if not runtime.is_file():
        raise BuildError(
            "builder.python_toolchain_unavailable",
            "Python candidate's runtime executable is not a regular file",
        )
    return _PythonProbe(
        runtime_executable=str(runtime),
        runtime_digest=executable_file_digest(runtime),
        implementation=raw["implementation"],
        version=raw["version"],
        version_info=tuple(raw_version),
        cache_tag=raw["cache_tag"],
    )


def _parse_python_command(raw: str, *, label: str) -> tuple[str, ...]:
    try:
        parsed = tuple(shlex.split(raw, posix=os.name != "nt"))
    except ValueError as exc:
        raise BuildError(
            "builder.python_toolchain", f"{label} is not a valid command"
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
        raise BuildError("builder.python_toolchain", f"{label} is empty")
    return parsed


def _explicit_python_command(
    pinned_command: str | Sequence[str] | None,
    environment: Mapping[str, str],
) -> tuple[str, ...] | None:
    if pinned_command is not None:
        if isinstance(pinned_command, str):
            return _parse_python_command(pinned_command, label="pinned Python command")
        command = tuple(pinned_command)
        if not command or any(
            not isinstance(item, str) or not item for item in command
        ):
            raise BuildError(
                "builder.python_toolchain", "pinned Python command is invalid"
            )
        return command
    raw = environment.get("PYTHON", "").strip()
    return _parse_python_command(raw, label="PYTHON") if raw else None


def _python_candidates(
    environment: Mapping[str, str],
    explicit: tuple[str, ...] | None,
):
    if explicit is not None:
        yield explicit
        return
    path_value = environment.get("PATH", os.defpath)
    seen: set[str] = set()
    for directory in path_value.split(os.pathsep):
        search_directory = directory or os.curdir
        for name in ("python3", "python"):
            found = shutil.which(name, path=search_directory)
            if found is None:
                continue
            invocation = str(Path(os.path.abspath(found)))
            key = os.path.normcase(invocation)
            if key in seen:
                continue
            seen.add(key)
            yield (invocation,)


def _resolved_python_command(
    candidate: tuple[str, ...], environment: Mapping[str, str]
) -> tuple[tuple[str, ...], Path]:
    found = shutil.which(candidate[0], path=environment.get("PATH"))
    if found is None:
        raise BuildError(
            "builder.python_toolchain_unavailable",
            "Python command was not found on PATH",
        )
    invocation = Path(os.path.abspath(found))
    try:
        launcher = invocation.resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.python_toolchain_unavailable",
            "Python launcher became unavailable during discovery",
        ) from exc
    if not launcher.is_file():
        raise BuildError(
            "builder.python_toolchain_unavailable",
            "Python launcher is not a regular file",
        )
    return (str(invocation), *candidate[1:]), launcher


def discover_python_toolchain(
    environment: Mapping[str, str] | None = None,
    *,
    pinned_command: str | Sequence[str] | None = None,
    minimum_version: tuple[int, ...] = (3, 11),
    required_version: tuple[int, ...] | None = None,
    timeout_seconds: float = DEFAULT_PYTHON_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_PYTHON_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_PYTHON_STDERR_LIMIT_BYTES,
) -> PythonToolchain:
    """Select an exact Python 3 command, honoring pins before ordered ``PATH``.

    A lifecycle caller uses ``pinned_command`` and ``required_version`` only after it
    has resolved those constraints from a Component or Flavor; this adapter does not
    parse specification prose. That command is authoritative. Otherwise a non-empty
    operator-provided ``PYTHON`` command is authoritative. With neither, each ``PATH``
    directory is considered in order and ``python3`` then ``python`` are probed within
    that directory. Invalid unpinned candidates are skipped; invalid explicit commands
    fail without fallback. The default minimum matches the framework and portable-
    Python Flavor requirement; callers may tighten it or provide an exact version
    prefix from a resolved pin.
    """

    configured = dict(os.environ if environment is None else environment)
    explicit = _explicit_python_command(pinned_command, configured)
    for label, constraint in (
        ("minimum", minimum_version),
        ("required", required_version),
    ):
        if constraint is not None and (
            not constraint
            or len(constraint) > 3
            or any(type(item) is not int or item < 0 for item in constraint)
            or constraint[0] != 3
        ):
            raise ValueError(
                f"{label} Python version must be a Python 3 version prefix"
            )
    last_error: BuildError | None = None
    for candidate in _python_candidates(configured, explicit):
        try:
            command, launcher = _resolved_python_command(candidate, configured)
            launcher_digest = executable_file_digest(launcher)
            probe = _probe_python_command(
                command,
                configured,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
            if probe.version_info[: len(minimum_version)] < minimum_version:
                raise BuildError(
                    "builder.python_version_unsupported",
                    "Python candidate is older than the required minimum version",
                )
            if (
                required_version is not None
                and probe.version_info[: len(required_version)] != required_version
            ):
                raise BuildError(
                    "builder.python_version_unsupported",
                    "Python candidate does not match the required version",
                )
            try:
                current_launcher = Path(command[0]).resolve(strict=True)
            except OSError as exc:
                raise BuildError(
                    "builder.python_toolchain_changed",
                    "Python launcher became unavailable during discovery",
                ) from exc
            if (
                current_launcher != launcher
                or executable_file_digest(current_launcher) != launcher_digest
            ):
                raise BuildError(
                    "builder.python_toolchain_changed",
                    "Python launcher changed during toolchain discovery",
                )
            return PythonToolchain(
                command=command,
                launcher_executable=str(launcher),
                launcher_digest=launcher_digest,
                runtime_executable=probe.runtime_executable,
                runtime_digest=probe.runtime_digest,
                implementation=probe.implementation,
                version=probe.version,
                version_info=probe.version_info,
                cache_tag=probe.cache_tag,
            )
        except BuildError as exc:
            if explicit is not None:
                raise
            last_error = exc
    error = BuildError(
        "builder.python_toolchain_unavailable",
        "no supported Python 3 runtime was found through PYTHON or ordered PATH",
    )
    if last_error is not None:
        raise error from last_error
    raise error


def require_unsandboxed_host_build_authorization(
    request: BuildRequest,
    authorization: BuildAuthorization,
) -> None:
    """Reject any grant that describes these ambient-host builders as constrained."""

    if request.sandbox_profile != UNSANDBOXED_HOST_BUILD_PROFILE:
        raise BuildError(
            "builder.unsandboxed_host_profile_required",
            "Host builders require the explicit unsandboxed-host build profile",
        )
    if tuple(sorted(request.requested_privileges)) != (
        UNSANDBOXED_HOST_BUILD_PRIVILEGES
    ):
        raise BuildError(
            "builder.ambient_privileges_required",
            "Host builders require the complete ambient host privilege set",
        )
    if authorization.profile is not SecurityProfile.YOLO or not authorization.warning:
        raise BuildError(
            "builder.yolo_authorization_required",
            "Unsandboxed host builds require an explicitly acknowledged YOLO grant",
        )
    if authorization.privileges != UNSANDBOXED_HOST_BUILD_PRIVILEGES:
        raise BuildError(
            "builder.authorization_privilege_mismatch",
            "Build authorization does not carry every ambient host privilege",
        )


def canonical_tree_digest(root: Path) -> str:
    root = root.resolve(strict=True)
    entries: list[dict[str, object]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BuildError(
                "builder.source_symlink", f"Source symlink rejected: {path}"
            )
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            content = path.read_bytes()
            entries.append(
                {
                    "path": relative,
                    "size": len(content),
                    "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
                }
            )
    entries.sort(key=lambda entry: str(entry["path"]))
    payload = json.dumps(
        entries,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _require_source_tree_unchanged(root: Path, expected_digest: str) -> None:
    if canonical_tree_digest(root) != expected_digest:
        raise BuildError(
            "builder.source_changed_during_build",
            "Source tree changed during compilation",
        )


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    artifact_digest: str
    artifact_path: Path
    source_bundle_digest: str
    authorization_id: str
    compiled_files: tuple[str, ...]
    toolchain_identity: str


class GuardedPythonBuilder:
    """Compile only after exact authorization and source-tree checks pass."""

    builder_id = "builder:python-bytecode@1"

    def __init__(
        self,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        toolchain: PythonToolchain | None = None,
        timeout_seconds: float = DEFAULT_PYTHON_BUILD_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_PYTHON_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_PYTHON_STDERR_LIMIT_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Python build timeout must be positive")
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("Python compiler output limits must be positive")
        self.authorization_verifier = (
            authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self.toolchain = toolchain or discover_python_toolchain()
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
    ) -> BuildArtifact:
        self.authorization_verifier.require_build_valid(authorization, request, now=now)
        require_unsandboxed_host_build_authorization(request, authorization)
        if request.builder_id != self.builder_id:
            raise BuildError(
                "builder.identity_mismatch", "Build request selected another builder"
            )
        if "python-bytecode" not in request.allowed_outputs:
            raise BuildError(
                "builder.output_not_authorized",
                "Python bytecode output is not authorized",
            )
        if request.toolchain_digest != self.toolchain.identity:
            raise BuildError(
                "builder.toolchain_mismatch",
                "Build request selected another Python toolchain",
            )
        environment = controlled_python_environment(dict(os.environ))
        self.toolchain.require_unchanged(environment)
        actual_source_digest = canonical_tree_digest(source_root)
        if actual_source_digest != request.source_bundle_digest:
            raise BuildError(
                "builder.source_digest_mismatch",
                "Source tree changed after authorization",
            )
        destination_root = artifact_store.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="python-build-", dir=destination_root))
        compiled: list[str] = []
        try:
            resolved_source = source_root.resolve(strict=True)
            for source in sorted(resolved_source.rglob("*.py")):
                if source.is_symlink():
                    raise BuildError(
                        "builder.source_symlink",
                        f"Source symlink rejected: {source}",
                    )
                relative = source.relative_to(resolved_source)
                target = staging / relative.with_suffix(".pyc")
                target.parent.mkdir(parents=True, exist_ok=True)
                self.toolchain.require_unchanged(environment)
                try:
                    completed = _run_bounded_process(
                        [
                            *self.toolchain.command,
                            "-I",
                            "-S",
                            "-c",
                            _PYTHON_COMPILE_SCRIPT,
                            str(source),
                            str(target),
                            relative.as_posix(),
                        ],
                        cwd=resolved_source,
                        environment=environment,
                        timeout_seconds=self.timeout_seconds,
                        stdout_limit_bytes=self.stdout_limit_bytes,
                        stderr_limit_bytes=self.stderr_limit_bytes,
                        error_prefix="builder.python_compile",
                    )
                finally:
                    self.toolchain.require_unchanged(environment)
                if completed.returncode != 0 or not target.is_file():
                    detail = (completed.stdout + completed.stderr)[-4000:].decode(
                        "utf-8", errors="replace"
                    )
                    raise BuildError(
                        "builder.python_compile_failed",
                        "Python bytecode compilation failed: " + detail,
                    )
                compiled.append(target.relative_to(staging).as_posix())
            _require_source_tree_unchanged(source_root, actual_source_digest)
            manifest = {
                "builder_id": self.builder_id,
                "source_bundle_digest": actual_source_digest,
                "authorization_id": authorization.authorization_id,
                "toolchain_identity": self.toolchain.identity,
                "compiled_files": compiled,
                "files": [
                    {
                        "path": path.relative_to(staging).as_posix(),
                        "digest": (
                            f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
                        ),
                    }
                    for path in sorted(staging.rglob("*.pyc"))
                ],
            }
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode()
            artifact_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            (staging / "build-manifest.json").write_bytes(manifest_bytes)
            if not _cached_artifact_matches(staging, manifest, manifest_bytes):
                raise BuildError(
                    "builder.artifact_invalid",
                    "Built artifact differs from its exact manifest",
                )
            final = destination_root / artifact_digest.removeprefix("sha256:")
            if final.exists():
                if not _cached_artifact_matches(final, manifest, manifest_bytes):
                    raise BuildError(
                        "builder.artifact_collision", "Existing artifact differs"
                    )
                shutil.rmtree(staging)
            else:
                os.replace(staging, final)
            return BuildArtifact(
                artifact_digest=artifact_digest,
                artifact_path=final,
                source_bundle_digest=actual_source_digest,
                authorization_id=authorization.authorization_id,
                compiled_files=tuple(compiled),
                toolchain_identity=self.toolchain.identity,
            )
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise


def _cached_artifact_matches(
    root: Path,
    manifest: dict[str, object],
    manifest_bytes: bytes,
) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    existing_manifest = root / "build-manifest.json"
    if (
        existing_manifest.is_symlink()
        or not existing_manifest.is_file()
        or existing_manifest.read_bytes() != manifest_bytes
    ):
        return False
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        return False
    expected = {
        str(item["path"]): str(item["digest"])
        for item in raw_files
        if isinstance(item, dict) and set(item) == {"path", "digest"}
    }
    if len(expected) != len(raw_files):
        return False
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            return False
        if path.is_dir():
            continue
        if not path.is_file():
            return False
        relative = path.relative_to(root).as_posix()
        if relative == "build-manifest.json":
            continue
        actual[relative] = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    return actual == expected


__all__ = [
    "BuildArtifact",
    "BuildError",
    "GuardedPythonBuilder",
    "PythonToolchain",
    "UNSANDBOXED_HOST_BUILD_PRIVILEGES",
    "UNSANDBOXED_HOST_BUILD_PROFILE",
    "canonical_tree_digest",
    "discover_python_toolchain",
    "executable_file_digest",
    "require_unsandboxed_host_build_authorization",
]
