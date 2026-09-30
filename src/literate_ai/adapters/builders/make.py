"""Exact GNU Make host-toolchain discovery for the Standard lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ._process import run_bounded_process
from .python import BuildError, executable_file_digest

DEFAULT_MAKE_VERSION_TIMEOUT_SECONDS = 10.0
DEFAULT_MAKE_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_MAKE_STDERR_LIMIT_BYTES = 1024 * 1024
DEFAULT_MAKE_MINIMUM_VERSION = (3, 81)

_GNU_MAKE_VERSION = re.compile(
    r"^GNU Make (?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)(?:\."
    r"(?P<patch>0|[1-9][0-9]*))?(?:[^0-9].*)?$"
)


def _identity(
    *,
    command: tuple[str, ...],
    launcher_executable: str,
    launcher_digest: str,
    version: str,
    version_info: tuple[int, int, int],
) -> str:
    encoded = json.dumps(
        {
            "command": list(command),
            "launcher_digest": launcher_digest,
            "launcher_executable": launcher_executable,
            "version": version,
            "version_info": list(version_info),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class MakeToolchain:
    """One GNU Make command bound to exact launcher bytes and version output."""

    command: tuple[str, ...]
    launcher_executable: str
    launcher_digest: str
    version: str
    version_info: tuple[int, int, int]
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
            or not isinstance(self.launcher_executable, str)
            or not self.launcher_executable
            or not isinstance(self.version, str)
            or _GNU_MAKE_VERSION.fullmatch(f"GNU Make {self.version}") is None
            or not isinstance(self.version_info, tuple)
            or len(self.version_info) != 3
            or any(type(item) is not int or item < 0 for item in self.version_info)
        ):
            raise ValueError("Make toolchain requires one exact GNU Make command")
        encoded = self.launcher_digest.removeprefix("sha256:")
        if (
            not self.launcher_digest.startswith("sha256:")
            or len(encoded) != 64
            or encoded != encoded.casefold()
        ):
            raise ValueError("Make launcher identity must be a sha256 digest")
        try:
            int(encoded, 16)
        except ValueError as exc:
            raise ValueError("Make launcher identity must be a sha256 digest") from exc
        object.__setattr__(
            self,
            "identity",
            _identity(
                command=self.command,
                launcher_executable=self.launcher_executable,
                launcher_digest=self.launcher_digest,
                version=self.version,
                version_info=self.version_info,
            ),
        )

    def require_unchanged(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        timeout_seconds: float = DEFAULT_MAKE_VERSION_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_MAKE_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_MAKE_STDERR_LIMIT_BYTES,
    ) -> None:
        configured = dict(os.environ if environment is None else environment)
        try:
            launcher = Path(self.command[0]).resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.make_toolchain_changed",
                "GNU Make launcher became unavailable after selection",
            ) from exc
        if str(launcher) != self.launcher_executable or not launcher.is_file():
            raise BuildError(
                "builder.make_toolchain_changed",
                "GNU Make launcher path changed after selection",
            )
        if executable_file_digest(launcher) != self.launcher_digest:
            raise BuildError(
                "builder.make_toolchain_changed",
                "GNU Make launcher bytes changed after selection",
            )
        version, version_info = _probe_make(
            self.command,
            configured,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
        )
        if (version, version_info) != (self.version, self.version_info):
            raise BuildError(
                "builder.make_toolchain_changed",
                "GNU Make version changed after toolchain selection",
            )
        if executable_file_digest(launcher) != self.launcher_digest:
            raise BuildError(
                "builder.make_toolchain_changed",
                "GNU Make launcher bytes changed during version validation",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "launcher_executable": self.launcher_executable,
            "launcher_digest": self.launcher_digest,
            "version": self.version,
            "version_info": list(self.version_info),
            "identity": self.identity,
        }


def _parse_command(raw: str, *, label: str) -> tuple[str, ...]:
    try:
        command = tuple(shlex.split(raw, posix=os.name != "nt"))
    except ValueError as exc:
        raise BuildError("builder.make_toolchain", f"{label} is invalid") from exc
    if not command:
        raise BuildError("builder.make_toolchain", f"{label} is empty")
    return command


def _probe_make(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> tuple[str, tuple[int, int, int]]:
    completed = run_bounded_process(
        [*command, "--version"],
        cwd=None,
        environment=environment,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        error_prefix="builder.make_version",
    )
    if completed.returncode != 0:
        raise BuildError("builder.make_version_failed", "GNU Make version probe failed")
    try:
        first_line = completed.stdout.decode("utf-8").splitlines()[0]
    except (UnicodeDecodeError, IndexError) as exc:
        raise BuildError(
            "builder.make_version_failed",
            "GNU Make did not report one UTF-8 version line",
        ) from exc
    match = _GNU_MAKE_VERSION.fullmatch(first_line)
    if match is None:
        raise BuildError(
            "builder.make_version_unsupported",
            "candidate is not GNU Make",
        )
    values = tuple(
        int(match.group(name) or "0") for name in ("major", "minor", "patch")
    )
    return ".".join(
        str(item) for item in values[: 3 if match.group("patch") else 2]
    ), values


def _validated_version_constraint(
    value: tuple[int, ...] | None, *, label: str
) -> tuple[int, ...] | None:
    if value is not None and (
        not value
        or len(value) > 3
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ValueError(f"{label} GNU Make version must be a numeric version prefix")
    return value


def discover_make_toolchain(
    environment: Mapping[str, str] | None = None,
    *,
    pinned_command: str | Sequence[str] | None = None,
    minimum_version: tuple[int, ...] = DEFAULT_MAKE_MINIMUM_VERSION,
    required_version: tuple[int, ...] | None = None,
    timeout_seconds: float = DEFAULT_MAKE_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_MAKE_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_MAKE_STDERR_LIMIT_BYTES,
) -> MakeToolchain:
    """Select exact GNU Make, honoring an explicit pin before ordered ``PATH``."""

    configured = dict(os.environ if environment is None else environment)
    minimum = _validated_version_constraint(minimum_version, label="minimum")
    required = _validated_version_constraint(required_version, label="required")
    explicit: tuple[str, ...] | None = None
    if pinned_command is not None:
        explicit = (
            _parse_command(pinned_command, label="pinned Make command")
            if isinstance(pinned_command, str)
            else tuple(pinned_command)
        )
        if not explicit or any(
            not isinstance(item, str) or not item for item in explicit
        ):
            raise BuildError("builder.make_toolchain", "pinned Make command is invalid")
    elif configured.get("MAKE", "").strip():
        explicit = _parse_command(configured["MAKE"], label="MAKE")
    candidates = (explicit,) if explicit is not None else (("make",), ("gmake",))
    last_error: BuildError | None = None
    for candidate in candidates:
        assert candidate is not None
        found = shutil.which(candidate[0], path=configured.get("PATH"))
        if found is None:
            last_error = BuildError(
                "builder.make_toolchain_unavailable",
                "GNU Make command was not found on PATH",
            )
            if explicit is not None:
                raise last_error
            continue
        invocation = Path(os.path.abspath(found))
        try:
            launcher = invocation.resolve(strict=True)
        except OSError as exc:
            last_error = BuildError(
                "builder.make_toolchain_unavailable",
                "GNU Make launcher became unavailable during discovery",
            )
            if explicit is not None:
                raise last_error from exc
            continue
        command = (str(invocation), *candidate[1:])
        try:
            version, version_info = _probe_make(
                command,
                configured,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
            assert minimum is not None
            if version_info[: len(minimum)] < minimum:
                raise BuildError(
                    "builder.make_version_unsupported",
                    "GNU Make candidate is older than the required minimum version",
                )
            if required is not None and version_info[: len(required)] != required:
                raise BuildError(
                    "builder.make_version_unsupported",
                    "GNU Make candidate does not match the required version",
                )
            toolchain = MakeToolchain(
                command,
                str(launcher),
                executable_file_digest(launcher),
                version,
                version_info,
            )
            toolchain.require_unchanged(
                configured,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
            return toolchain
        except BuildError as exc:
            last_error = exc
            if explicit is not None:
                raise
    if last_error is not None:
        raise last_error
    raise BuildError(
        "builder.make_toolchain_unavailable", "GNU Make command was not found"
    )


__all__ = [
    "DEFAULT_MAKE_MINIMUM_VERSION",
    "DEFAULT_MAKE_STDERR_LIMIT_BYTES",
    "DEFAULT_MAKE_STDOUT_LIMIT_BYTES",
    "DEFAULT_MAKE_VERSION_TIMEOUT_SECONDS",
    "MakeToolchain",
    "discover_make_toolchain",
]
