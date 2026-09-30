"""Authorization-gated portable C++ builder and host toolchain discovery."""

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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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

# Toolchain startup can exceed the ordinary command-probe budget while Xcode or
# Bazel is active in the parallel sample matrix.  Discovery is still bounded,
# but should not reject a healthy compiler solely because macOS is under load.
DEFAULT_CPP_VERSION_TIMEOUT_SECONDS = 60.0
DEFAULT_CPP_BUILD_TIMEOUT_SECONDS = 120.0
DEFAULT_COMPILER_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_COMPILER_STDERR_LIMIT_BYTES = 1024 * 1024
_CYCLONEDX_VERSION_LIMIT = 1024
_MSVC_VERSION = re.compile(
    r"^Microsoft \(R\) C/C\+\+ Optimizing Compiler Version "
    r"(?P<version>[0-9]+(?:\.[0-9]+)+) for [^\s]+$"
)


def _is_test_translation_unit(path: Path, *, source_root: Path) -> bool:
    relative = path.relative_to(source_root)
    stem = relative.stem.casefold()
    return (
        any(part.casefold() in {"test", "tests"} for part in relative.parts[:-1])
        or stem in {"test", "tests"}
        or stem.startswith("test_")
        or stem.endswith("_test")
    )


def _is_embedded_generated_test_translation_unit(
    path: Path, *, source_root: Path
) -> bool:
    """Identify the one generated-test implementation linked into the artifact.

    Standalone test programs remain excluded because they may define their own
    ``main``.  The canonical embedded unit instead implements the framework-owned
    ``--litai-test`` and ``--litai-smoke`` modes required after source custody is gone.
    """

    return path.relative_to(source_root).as_posix() == "source/tests/litai_test.cpp"


@dataclass(frozen=True, slots=True)
class CppToolchain:
    """One resolved host C++ compiler with an exact executable-byte identity.

    The executable is checked immediately before and after every compiler invocation.
    Like the portable Python builder, this detects temporal drift but cannot make the
    host filesystem lookup and process creation one atomic operating-system operation.
    """

    command: tuple[str, ...]
    family: str
    version: str
    executable_digest: str
    environment: tuple[tuple[str, str], ...] = ()
    sdk_selection_identity: str | None = None
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
            or self.family not in {"gnu", "msvc"}
            or any(
                not isinstance(name, str)
                or not name
                or not isinstance(value, str)
                or not value
                for name, value in self.environment
            )
            or len({name.casefold() for name, _value in self.environment})
            != len(self.environment)
        ):
            raise ValueError("C++ toolchain must identify a supported compiler")
        if (
            not isinstance(self.version, str)
            or not self.version
            or not isinstance(self.executable_digest, str)
            or not self.executable_digest.startswith("sha256:")
        ):
            raise ValueError("C++ toolchain requires versioned identity")
        encoded_digest = self.executable_digest.removeprefix("sha256:")
        if len(encoded_digest) != 64 or encoded_digest != encoded_digest.casefold():
            raise ValueError("C++ executable identity must be a sha256 digest")
        try:
            int(encoded_digest, 16)
        except ValueError as exc:
            raise ValueError("C++ executable identity must be a sha256 digest") from exc
        object.__setattr__(
            self,
            "identity",
            _toolchain_identity(
                self.command,
                self.family,
                self.version,
                self.executable_digest,
                self.environment,
                sdk_selection_identity=self.sdk_selection_identity,
            ),
        )

    def require_unchanged(self) -> None:
        if self.sdk_selection_identity is not None:
            if _sdk_selection_identity(self.environment) != self.sdk_selection_identity:
                raise BuildError(
                    "builder.cpp_toolchain_changed",
                    "Selected macOS SDK metadata or directory changed",
                )
        configured = Path(self.command[0])
        try:
            executable = configured.resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.cpp_toolchain_changed",
                "C++ compiler became unavailable after toolchain selection",
            ) from exc
        if str(executable) != self.command[0] or not executable.is_file():
            raise BuildError(
                "builder.cpp_toolchain_changed",
                "C++ compiler path changed after toolchain selection",
            )
        if executable_file_digest(executable) != self.executable_digest:
            raise BuildError(
                "builder.cpp_toolchain_changed",
                "C++ compiler bytes changed after toolchain selection",
            )


def _macos_sdk_environment(
    configured: dict[str, str], *, timeout_seconds: float
) -> tuple[tuple[str, str], ...]:
    """Resolve implicit Apple selections once, then pin them for every invocation."""
    selected = dict(configured)
    for name, command in (
        ("DEVELOPER_DIR", ("/usr/bin/xcode-select", "--print-path")),
        ("SDKROOT", ("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path")),
    ):
        if not selected.get(name):
            result = _run_bounded_process(
                command,
                cwd=None,
                environment=selected,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=16 * 1024,
                stderr_limit_bytes=16 * 1024,
                error_prefix="builder.cpp_sdk",
            )
            try:
                value = result.stdout.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise BuildError(
                    "builder.cpp_sdk", "Invalid SDK discovery output"
                ) from exc
            if result.returncode or not value or len(value.splitlines()) != 1:
                raise BuildError("builder.cpp_sdk", "Cannot resolve selected macOS SDK")
            selected[name] = value
    environment = tuple((name, selected[name]) for name in ("SDKROOT", "DEVELOPER_DIR"))
    _sdk_selection_identity(environment)
    return environment


def bazel_sdk_build_options(
    environment: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Declare the selected SDK to Bazel repository probes and compile actions."""
    selected = dict(environment)
    options = tuple(
        f"--{scope}={name}={selected[name]}"
        for name in ("SDKROOT", "DEVELOPER_DIR")
        if name in selected
        for scope in ("repo_env", "action_env", "host_action_env")
    )
    if "SDKROOT" not in selected:
        return options
    sdk = selected["SDKROOT"].replace("%", "%%").replace(":", "%:")
    return options + (
        f"--repo_env=BAZEL_CXXOPTS=-std=c++17:-isystem:{sdk}",
        f"--repo_env=BAZEL_CONLYOPTS=-isystem:{sdk}",
    )


def _sdk_selection_identity(environment: tuple[tuple[str, str], ...]) -> str:
    """Bind selected SDK roots and bounded SDK metadata, not the entire SDK tree."""
    selected = dict(environment)
    records = {}
    try:
        for name in ("SDKROOT", "DEVELOPER_DIR"):
            if name not in selected:
                continue
            path = Path(selected[name])
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("SDK selection must name an absolute directory")
            record = {"path": str(path), "resolved": str(path.resolve(strict=True))}
            if name == "SDKROOT":
                metadata = path / "SDKSettings.json"
                descriptor = os.open(
                    metadata, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                )
                with os.fdopen(descriptor, "rb") as stream:
                    status = os.fstat(stream.fileno())
                    if not stat.S_ISREG(status.st_mode) or status.st_size > 1024 * 1024:
                        raise ValueError("SDK metadata must be a bounded regular file")
                    content = stream.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    raise ValueError("SDK metadata exceeds its bound")
                record["metadata"] = hashlib.sha256(content).hexdigest()
            records[name] = record
    except (OSError, ValueError) as exc:
        raise BuildError(
            "builder.cpp_toolchain_changed",
            "Selected macOS SDK is unavailable or invalid",
        ) from exc
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _compiler_family(command: str) -> str:
    name = Path(command).name.casefold()
    if name in {"cl", "cl.exe"} or "clang-cl" in name:
        return "msvc"
    return "gnu"


def _default_cpp_compiler_names(os_name: str) -> tuple[str, ...]:
    """Choose the platform-native compiler family unless ``CXX`` overrides it."""

    if os_name == "nt":
        return ("cl", "clang-cl")
    return ("c++", "clang++", "g++")


_MSVC_TOOLSET_PATH = re.compile(r"(?i)([\\/]VC[\\/]Tools[\\/]MSVC[\\/])([^;\\/]+)")


def _msvc_toolset_root(compiler: Path) -> Path | None:
    for directory in compiler.parents:
        if directory.parent.name.casefold() != "msvc":
            continue
        if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)*", directory.name):
            raise BuildError(
                "builder.cpp_msvc_environment_unavailable",
                "MSVC compiler path contains an invalid toolset version",
            )
        return directory
    return None


def _msvc_toolset_argument(compiler: Path) -> str | None:
    directory = _msvc_toolset_root(compiler)
    if directory is not None:
        major, minor, *_patch = directory.name.split(".")
        return f"-vcvars_ver={major}.{minor}"
    return None


def _pin_msvc_toolset_paths(
    environment: dict[str, str], compiler: Path
) -> dict[str, str]:
    toolset = _msvc_toolset_root(compiler)
    if toolset is None:
        return environment
    return {
        name: _MSVC_TOOLSET_PATH.sub(
            lambda match: match.group(1) + toolset.name,
            value,
        )
        for name, value in environment.items()
    }


def _windows_msvc_environment(
    compiler: Path,
    configured: dict[str, str],
    *,
    timeout_seconds: float = DEFAULT_CPP_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_COMPILER_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_COMPILER_STDERR_LIMIT_BYTES,
) -> tuple[tuple[str, str], ...]:
    """Resolve the complete x64 MSVC command environment from vendor tooling."""

    normalized = {name.casefold(): value for name, value in configured.items()}
    required = ("include", "lib", "libpath", "path")
    setup_candidates: list[Path] = []
    for ancestor in compiler.parents:
        setup_candidates.extend(
            (
                ancestor / "Auxiliary" / "Build" / "vcvarsall.bat",
                ancestor / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat",
                ancestor / "Auxiliary" / "Build" / "vcvars64.bat",
                ancestor / "VC" / "Auxiliary" / "Build" / "vcvars64.bat",
            )
        )
    setup = next((path for path in setup_candidates if path.is_file()), None)
    clang_cl = compiler.stem.casefold() == "clang-cl"
    if setup is None and clang_cl:
        # LLVM's clang-cl can be present without Visual Studio. In that case there is
        # no vcvars closure to bind; the later compile probe decides whether the
        # standalone installation is actually usable.
        return ()
    if setup is None and all(normalized.get(name, "").strip() for name in required):
        return tuple((name.upper(), normalized[name]) for name in required)
    system_root = normalized.get("systemroot", "C:/Windows")
    command_interpreter = Path(system_root) / "System32" / "cmd.exe"
    if setup is None or not command_interpreter.is_file():
        raise BuildError(
            "builder.cpp_msvc_environment_unavailable",
            "MSVC was found but its x64 developer environment is unavailable",
        )
    toolset_argument = _msvc_toolset_argument(compiler)
    vcvarsall = setup.name.casefold() == "vcvarsall.bat"
    if toolset_argument is not None and not vcvarsall:
        raise BuildError(
            "builder.cpp_msvc_environment_unavailable",
            "versioned MSVC requires the vendor vcvarsall environment",
        )
    with tempfile.TemporaryDirectory(prefix="litai-msvc-environment-") as directory:
        wrapper = Path(directory) / "initialize.cmd"
        wrapper.write_text(
            '@call "%~1" %~2 %~3 >nul 2>&1\n'
            "@if errorlevel 1 exit /b %errorlevel%\n@set\n",
            encoding="utf-8",
            newline="\r\n",
        )
        command = [
            str(command_interpreter.resolve()),
            "/d",
            "/s",
            "/c",
            str(wrapper),
            str(setup.resolve()),
        ]
        if vcvarsall:
            command.append("x64")
        if toolset_argument is not None:
            command.append(toolset_argument)
        completed = _run_bounded_process(
            command,
            cwd=None,
            environment=configured,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            error_prefix="builder.cpp_msvc_environment",
        )
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr)[-4000:].decode(
            "utf-8", errors="replace"
        )
        raise BuildError(
            "builder.cpp_msvc_environment_unavailable",
            "MSVC x64 developer environment initialization failed: " + detail,
        )
    resolved: dict[str, str] = {}
    for raw_line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        if "=" not in raw_line or raw_line.startswith("="):
            continue
        name, value = raw_line.split("=", 1)
        folded = name.casefold()
        if folded in required and value.strip():
            resolved[folded] = value
    if any(name not in resolved for name in required):
        raise BuildError(
            "builder.cpp_msvc_environment_unavailable",
            "MSVC x64 developer environment omitted required compiler variables",
        )
    resolved = _pin_msvc_toolset_paths(resolved, compiler)
    return tuple((name.upper(), resolved[name]) for name in required)


def _compiler_process_environment(
    configured: dict[str, str], toolchain: CppToolchain
) -> dict[str, str]:
    environment = dict(configured)
    if os.name == "nt":
        overridden = {name.casefold() for name, _value in toolchain.environment}
        environment = {
            name: value
            for name, value in environment.items()
            if name.casefold() not in overridden
        }
    environment.update(dict(toolchain.environment))
    return environment


def _gnu_linker_reproducibility_arguments(os_name: str) -> tuple[str, ...]:
    """Suppress the mutable PE timestamp for GNU-family Windows linkers."""

    return ("-Wl,--no-insert-timestamp",) if os_name == "nt" else ()


def _toolchain_identity(
    command: tuple[str, ...],
    family: str,
    version: str,
    executable_digest: str,
    environment: tuple[tuple[str, str], ...],
    sdk_selection_identity: str | None = None,
) -> str:
    encoded = json.dumps(
        {
            "command": list(command),
            "executable_digest": executable_digest,
            "family": family,
            "environment": dict(environment),
            "version": version,
            **(
                {"sdk_selection_identity": sdk_selection_identity}
                if sdk_selection_identity
                else {}
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _normalized_compiler_version(
    output: bytes, *, family: str, clang_cl: bool = False
) -> str:
    """Project verbose compiler banners into a standard-sized exact version."""

    text = output.decode("utf-8", errors="replace").strip()
    lines = tuple(line.strip() for line in text.splitlines() if line.strip())
    if family == "msvc" and not clang_cl:
        match = next(
            (
                candidate
                for line in lines
                if (candidate := _MSVC_VERSION.fullmatch(line)) is not None
            ),
            None,
        )
        if match is None:
            raise BuildError(
                "builder.cpp_version_unparseable",
                "MSVC version output omitted its exact compiler version",
            )
        return match.group("version")
    if family == "gnu" and lines and "Cuda compilation tools" in text:
        version_line = next(
            (line for line in lines if "Cuda compilation tools" in line), lines[-1]
        )
        if len(version_line) <= _CYCLONEDX_VERSION_LIMIT:
            return version_line
    if not lines or len(lines[0]) > _CYCLONEDX_VERSION_LIMIT:
        raise BuildError(
            "builder.cpp_version_unparseable",
            "C++ compiler version output omitted a bounded version banner",
        )
    return lines[0]


def discover_cpp_toolchain(
    environment: dict[str, str] | None = None,
    *,
    timeout_seconds: float = DEFAULT_CPP_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_COMPILER_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_COMPILER_STDERR_LIMIT_BYTES,
) -> CppToolchain:
    """Resolve ``CXX`` or the first portable host compiler found on ``PATH``."""

    configured = dict(os.environ if environment is None else environment)
    raw = configured.get("CXX", "").strip()
    candidates: tuple[tuple[str, ...], ...]
    if raw:
        try:
            parsed = tuple(shlex.split(raw, posix=os.name != "nt"))
        except ValueError as exc:
            raise BuildError(
                "builder.cpp_toolchain", "CXX is not a valid command"
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
            raise BuildError("builder.cpp_toolchain", "CXX is empty")
        candidates = (parsed,)
    else:
        names = _default_cpp_compiler_names(os.name)
        candidates = tuple((name,) for name in names)

    path_value = configured.get("PATH")
    for candidate in candidates:
        executable = shutil.which(candidate[0], path=path_value)
        if executable is None:
            continue
        command = (str(Path(executable).resolve()), *candidate[1:])
        family = _compiler_family(command[0])
        toolchain_environment = (
            _windows_msvc_environment(
                Path(command[0]),
                configured,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
            if os.name == "nt" and family == "msvc"
            else ()
        )
        sdk_selection_identity = None
        if sys.platform == "darwin" and os.name != "nt":
            toolchain_environment = _macos_sdk_environment(
                configured, timeout_seconds=timeout_seconds
            )
            configured.update(toolchain_environment)
            sdk_selection_identity = _sdk_selection_identity(toolchain_environment)
        clang_cl = Path(command[0]).stem.casefold() == "clang-cl"
        version_flag = "/?" if family == "msvc" and not clang_cl else "--version"
        try:
            executable_digest = executable_file_digest(Path(command[0]))
            completed = _run_bounded_process(
                [*command, version_flag],
                cwd=None,
                environment=configured,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
                error_prefix="builder.cpp_version",
            )
            if executable_file_digest(Path(command[0])) != executable_digest:
                raise BuildError(
                    "builder.cpp_toolchain_changed",
                    "C++ compiler bytes changed during toolchain discovery",
                )
        except BuildError:
            if raw:
                raise
            continue
        version = _normalized_compiler_version(
            completed.stdout + completed.stderr,
            family=family,
            clang_cl=clang_cl,
        )
        if (
            sdk_selection_identity is not None
            and _sdk_selection_identity(toolchain_environment) != sdk_selection_identity
        ):
            raise BuildError(
                "builder.cpp_toolchain_changed",
                "Selected macOS SDK changed during discovery",
            )
        return CppToolchain(
            command,
            family,
            version,
            executable_digest,
            toolchain_environment,
            sdk_selection_identity,
        )
    raise BuildError(
        "builder.cpp_toolchain_unavailable",
        "no supported C++ compiler was found through CXX or PATH",
    )


@dataclass(frozen=True, slots=True)
class NativeBuildArtifact:
    artifact_digest: str
    artifact_path: Path
    source_bundle_digest: str
    authorization_id: str
    executable_file: str
    compiler_identity: str


class GuardedCppBuilder:
    """Compile generated C++ only after exact build authorization checks pass."""

    builder_id = "builder:cpp-native@1"

    def __init__(
        self,
        toolchain: CppToolchain,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        timeout_seconds: float = DEFAULT_CPP_BUILD_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_COMPILER_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_COMPILER_STDERR_LIMIT_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("C++ build timeout must be positive")
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("C++ compiler output limits must be positive")
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
                "Build request selected another C++ toolchain",
            )
        self.toolchain.require_unchanged()
        actual_source_digest = canonical_tree_digest(source_root)
        if actual_source_digest != request.source_bundle_digest:
            raise BuildError(
                "builder.source_digest_mismatch",
                "Source tree changed after authorization",
            )

        resolved_source = source_root.resolve(strict=True)
        discovered_translation_units = tuple(
            sorted(
                path
                for path in resolved_source.rglob("*")
                if path.is_file() and path.suffix.casefold() in {".cc", ".cpp", ".cxx"}
            )
        )
        translation_units = tuple(
            path
            for path in discovered_translation_units
            if not _is_test_translation_unit(path, source_root=resolved_source)
            or _is_embedded_generated_test_translation_unit(
                path, source_root=resolved_source
            )
        )
        if not translation_units:
            raise BuildError(
                "builder.cpp_entrypoint_missing",
                "Generated C++ source has no application translation unit",
            )
        for source in discovered_translation_units:
            if source.is_symlink():
                raise BuildError(
                    "builder.source_symlink", f"Source symlink rejected: {source}"
                )

        destination_root = artifact_store.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="cpp-build-", dir=destination_root))
        executable_name = "sample.exe" if os.name == "nt" else "sample"
        executable = staging / executable_name
        relative_sources = [
            source.relative_to(resolved_source).as_posix()
            for source in translation_units
        ]
        try:
            command = self._compile_command(
                relative_sources,
                executable,
                staging,
                resolved_source,
            )
            try:
                completed = _run_bounded_process(
                    command,
                    cwd=resolved_source,
                    environment=_compiler_process_environment(
                        dict(os.environ), self.toolchain
                    ),
                    timeout_seconds=self.timeout_seconds,
                    stdout_limit_bytes=self.stdout_limit_bytes,
                    stderr_limit_bytes=self.stderr_limit_bytes,
                    error_prefix="builder.cpp_compile",
                )
            finally:
                self.toolchain.require_unchanged()
            _require_source_tree_unchanged(resolved_source, actual_source_digest)
            if completed.returncode != 0 or not executable.is_file():
                detail = (completed.stdout + completed.stderr)[-4000:].decode(
                    "utf-8", errors="replace"
                )
                if self._compile_and_link_canary(staging):
                    raise BuildError(
                        "builder.cpp_generated_source_rejected",
                        "C++ compiler rejected the generated source: " + detail,
                    )
                raise BuildError(
                    "builder.cpp_compile_failed",
                    "C++ compilation failed: " + detail,
                )
            for intermediate in staging.glob("*.obj"):
                intermediate.unlink()
            executable_digest = (
                f"sha256:{hashlib.sha256(executable.read_bytes()).hexdigest()}"
            )
            manifest = {
                "builder_id": self.builder_id,
                "source_bundle_digest": actual_source_digest,
                "authorization_id": authorization.authorization_id,
                "compiler_identity": self.toolchain.identity,
                "executable_file": executable_name,
                "executable_digest": executable_digest,
            }
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            artifact_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            (staging / "build-manifest.json").write_bytes(manifest_bytes)
            if not _cached_native_artifact_matches(
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
                if not _cached_native_artifact_matches(
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
            return NativeBuildArtifact(
                artifact_digest,
                final,
                actual_source_digest,
                authorization.authorization_id,
                executable_name,
                self.toolchain.identity,
            )
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def _compile_command(
        self,
        sources: list[str],
        executable: Path,
        object_directory: Path,
        include_directory: Path,
    ) -> list[str]:
        if self.toolchain.family == "msvc":
            return [
                *self.toolchain.command,
                "/nologo",
                "/std:c++17",
                "/EHsc",
                "/utf-8",
                "/O2",
                "/W4",
                "/Brepro",
                f"/I{include_directory}",
                f"/I{include_directory / 'source'}",
                *sources,
                f"/Fo:{object_directory}{os.sep}",
                f"/Fe:{executable}",
                "/link",
                "/INCREMENTAL:NO",
            ]
        return [
            *self.toolchain.command,
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-pedantic",
            "-I",
            str(include_directory),
            "-I",
            str(include_directory / "source"),
            *sources,
            *_gnu_linker_reproducibility_arguments(os.name),
            "-o",
            str(executable),
        ]

    def _compile_and_link_canary(self, staging: Path) -> bool:
        """Prove compiler/linker health before blaming generated C++ source."""

        canary = staging / "toolchain-canary"
        canary.mkdir()
        source = canary / "canary.cpp"
        source.write_text("int main() { return 0; }\n", encoding="utf-8", newline="")
        executable = canary / ("canary.exe" if os.name == "nt" else "canary")
        self.toolchain.require_unchanged()
        try:
            completed = _run_bounded_process(
                self._compile_command([source.name], executable, canary, canary),
                cwd=canary,
                environment=_compiler_process_environment(
                    dict(os.environ), self.toolchain
                ),
                timeout_seconds=self.timeout_seconds,
                stdout_limit_bytes=self.stdout_limit_bytes,
                stderr_limit_bytes=self.stderr_limit_bytes,
                error_prefix="builder.cpp_canary",
            )
        finally:
            self.toolchain.require_unchanged()
        return completed.returncode == 0 and executable.is_file()


def _cached_native_artifact_matches(
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
    "CppToolchain",
    "DEFAULT_COMPILER_STDERR_LIMIT_BYTES",
    "DEFAULT_COMPILER_STDOUT_LIMIT_BYTES",
    "DEFAULT_CPP_BUILD_TIMEOUT_SECONDS",
    "DEFAULT_CPP_VERSION_TIMEOUT_SECONDS",
    "GuardedCppBuilder",
    "NativeBuildArtifact",
    "discover_cpp_toolchain",
]
