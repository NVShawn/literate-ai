"""Exact Zig host-toolchain discovery for the Standard lifecycle.

``zig`` is the language toolchain. ``zig-cc`` is the same distribution invoked as
``zig cc`` / ``zig c++`` for C and C++. Both bind the Zig executable bytes and
``zig version`` output; extra driver arguments stay on the command vector and are
never interpolated through a shell.
"""

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

DEFAULT_ZIG_VERSION_TIMEOUT_SECONDS = 10.0
DEFAULT_ZIG_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_ZIG_STDERR_LIMIT_BYTES = 1024 * 1024
DEFAULT_ZIG_MINIMUM_VERSION = (0, 13)
_ZIG_CC_DRIVERS = frozenset({"cc", "c++"})
_ZIG_VERSION = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-dev\.[0-9A-Za-z.+-]+)?(?:\s.*)?$"
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


def _version_invocation(command: tuple[str, ...]) -> tuple[str, ...]:
    """Probe ``zig version``, stripping a trailing ``cc`` / ``c++`` driver token."""

    if len(command) > 1 and command[-1] in _ZIG_CC_DRIVERS:
        return (*command[:-1], "version")
    return (*command, "version")


@dataclass(frozen=True, slots=True)
class ZigToolchain:
    """One Zig command bound to exact launcher bytes and ``zig version`` output."""

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
            or _ZIG_VERSION.fullmatch(self.version) is None
            or not isinstance(self.version_info, tuple)
            or len(self.version_info) != 3
            or any(type(item) is not int or item < 0 for item in self.version_info)
        ):
            raise ValueError("Zig toolchain requires one exact zig command")
        digest = self.launcher_digest.removeprefix("sha256:")
        if (
            not self.launcher_digest.startswith("sha256:")
            or len(digest) != 64
            or digest != digest.casefold()
        ):
            raise ValueError("Zig launcher identity must be a sha256 digest")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("Zig launcher identity must be a sha256 digest") from exc
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
        timeout_seconds: float = DEFAULT_ZIG_VERSION_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_ZIG_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_ZIG_STDERR_LIMIT_BYTES,
    ) -> None:
        configured = dict(os.environ if environment is None else environment)
        try:
            launcher = Path(self.command[0]).resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.zig_toolchain_changed",
                "Zig launcher became unavailable after selection",
            ) from exc
        if str(launcher) != self.launcher_executable or not launcher.is_file():
            raise BuildError(
                "builder.zig_toolchain_changed",
                "Zig launcher path changed after selection",
            )
        if executable_file_digest(launcher) != self.launcher_digest:
            raise BuildError(
                "builder.zig_toolchain_changed",
                "Zig launcher bytes changed after selection",
            )
        version, version_info = _probe_zig(
            self.command,
            configured,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
        )
        if (version, version_info) != (self.version, self.version_info):
            raise BuildError(
                "builder.zig_toolchain_changed",
                "Zig version changed after toolchain selection",
            )
        if executable_file_digest(launcher) != self.launcher_digest:
            raise BuildError(
                "builder.zig_toolchain_changed",
                "Zig launcher bytes changed during version validation",
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
        raise BuildError("builder.zig_toolchain", f"{label} is invalid") from exc
    if not command:
        raise BuildError("builder.zig_toolchain", f"{label} is empty")
    return command


def _probe_zig(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> tuple[str, tuple[int, int, int]]:
    completed = run_bounded_process(
        list(_version_invocation(command)),
        cwd=None,
        environment=environment,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        error_prefix="builder.zig_version",
    )
    if completed.returncode != 0:
        raise BuildError("builder.zig_version_failed", "Zig version probe failed")
    try:
        line = completed.stdout.decode("utf-8").splitlines()[0].strip()
    except (UnicodeDecodeError, IndexError) as exc:
        raise BuildError(
            "builder.zig_version_failed",
            "Zig did not report one UTF-8 version line",
        ) from exc
    match = _ZIG_VERSION.fullmatch(line)
    if match is None:
        raise BuildError(
            "builder.zig_version_unsupported",
            "candidate is not Zig",
        )
    version_info = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    return line, version_info


def _validated_version_constraint(
    value: tuple[int, ...] | None, *, label: str
) -> tuple[int, ...] | None:
    if value is not None and (
        not value
        or len(value) > 3
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ValueError(f"{label} Zig version must be a numeric version prefix")
    return value


def discover_zig_toolchain(
    environment: Mapping[str, str] | None = None,
    *,
    pinned_command: str | Sequence[str] | None = None,
    minimum_version: tuple[int, ...] = DEFAULT_ZIG_MINIMUM_VERSION,
    required_version: tuple[int, ...] | None = None,
    default_command: Sequence[str] = ("zig",),
    timeout_seconds: float = DEFAULT_ZIG_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_ZIG_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_ZIG_STDERR_LIMIT_BYTES,
) -> ZigToolchain:
    """Select exact Zig, honoring an explicit pin before ordered ``PATH``.

    Discovery runs only ``zig version``. A trailing ``cc`` or ``c++`` driver token
    stays on the bound command and is omitted from that probe so ``zig cc`` is not
    asked to compile a file named ``version``.
    """

    configured = dict(os.environ if environment is None else environment)
    minimum = _validated_version_constraint(minimum_version, label="minimum")
    required = _validated_version_constraint(required_version, label="required")
    default = tuple(default_command)
    if not default or any(not isinstance(item, str) or not item for item in default):
        raise BuildError("builder.zig_toolchain", "default Zig command is invalid")
    explicit: tuple[str, ...] | None = None
    if pinned_command is not None:
        explicit = (
            _parse_command(pinned_command, label="pinned Zig command")
            if isinstance(pinned_command, str)
            else tuple(pinned_command)
        )
        if not explicit or any(
            not isinstance(item, str) or not item for item in explicit
        ):
            raise BuildError("builder.zig_toolchain", "pinned Zig command is invalid")
    elif configured.get("ZIG", "").strip():
        explicit = _parse_command(configured["ZIG"], label="ZIG")
    candidate = explicit or default
    found = shutil.which(candidate[0], path=configured.get("PATH"))
    if found is None:
        raise BuildError(
            "builder.zig_toolchain_unavailable",
            "Zig command was not found on PATH",
        )
    invocation = Path(os.path.abspath(found))
    try:
        launcher = invocation.resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.zig_toolchain_unavailable",
            "Zig launcher became unavailable during discovery",
        ) from exc
    if not launcher.is_file():
        raise BuildError(
            "builder.zig_toolchain_unavailable",
            "Zig does not resolve to a regular executable file",
        )
    command = (str(invocation), *candidate[1:])
    launcher_digest = executable_file_digest(launcher)
    version, version_info = _probe_zig(
        command,
        configured,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
    )
    assert minimum is not None
    if version_info[: len(minimum)] < minimum:
        raise BuildError(
            "builder.zig_version_unsupported",
            "Zig candidate is older than the required minimum version",
        )
    if required is not None and version_info[: len(required)] != required:
        raise BuildError(
            "builder.zig_version_unsupported",
            "Zig candidate does not match the required version",
        )
    try:
        current = invocation.resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.zig_toolchain_changed",
            "Zig launcher became unavailable during toolchain discovery",
        ) from exc
    if current != launcher or executable_file_digest(current) != launcher_digest:
        raise BuildError(
            "builder.zig_toolchain_changed",
            "Zig launcher bytes changed during toolchain discovery",
        )
    toolchain = ZigToolchain(
        command,
        str(launcher),
        launcher_digest,
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


__all__ = [
    "DEFAULT_ZIG_MINIMUM_VERSION",
    "DEFAULT_ZIG_STDERR_LIMIT_BYTES",
    "DEFAULT_ZIG_STDOUT_LIMIT_BYTES",
    "DEFAULT_ZIG_VERSION_TIMEOUT_SECONDS",
    "ZigToolchain",
    "discover_zig_toolchain",
]
