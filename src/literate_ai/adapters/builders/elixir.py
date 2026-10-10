"""Bound Elixir/OTP discovery for the Standard script-tree lifecycle."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts import canonical_identity

from ._process import run_bounded_process
from .python import BuildError, executable_file_digest

DEFAULT_ELIXIR_MINIMUM_VERSION = (1, 18)
DEFAULT_ELIXIR_VERSION_TIMEOUT_SECONDS = 15.0
# This is framework-owned code, never generated application code. It binds the
# selected installation's core modules as well as its launcher and VM binary.
_PROBE = """files = Enum.map([Kernel, JSON, :json], fn module ->
  Code.ensure_loaded!(module)
  case :code.which(module) do
    path when is_list(path) -> List.to_string(path)
    _ -> raise "required standard library is unavailable"
  end
end)
IO.puts(JSON.encode!(%{version: System.version(), otp: System.otp_release(),
  erts: List.to_string(:erlang.system_info(:version)),
  root: List.to_string(:code.root_dir()), files: files}))
"""
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-[A-Za-z0-9.+-]+)?$")


def _constraint(value: tuple[int, ...] | None) -> None:
    if value is not None and (
        not isinstance(value, tuple)
        or not 1 <= len(value) <= 3
        or any(type(part) is not int or part < 0 for part in value)
    ):
        raise ValueError("Elixir version constraints must be numeric prefixes")


def _probe(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    timeout: float,
    launcher_command: tuple[str, ...] | None = None,
):
    result = run_bounded_process(
        [*command, "-e", _PROBE],
        cwd=None,
        environment=environment,
        timeout_seconds=timeout,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
        error_prefix="builder.elixir_version",
    )
    try:
        if result.returncode:
            raise ValueError("probe failed")
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or set(data) != {
            "version",
            "otp",
            "erts",
            "root",
            "files",
        }:
            raise ValueError("invalid probe record")
        if any(
            not isinstance(data[key], str) for key in ("version", "otp", "erts", "root")
        ):
            raise ValueError("invalid probe strings")
        match = _VERSION.fullmatch(data["version"])
        if match is None or not data["otp"].isdigit() or int(data["otp"]) < 27:
            raise ValueError("requires Elixir and Erlang/OTP 27+")
        if not re.fullmatch(r"[0-9.]+", data["erts"]):
            raise ValueError("invalid ERTS version")
        if not isinstance(data["files"], list) or len(data["files"]) != 3:
            raise ValueError("missing standard library modules")
        vm_root = Path(data["root"]) / f"erts-{data['erts']}" / "bin"
        vm = next(
            (
                vm_root / name
                for name in ("beam.smp", "beam.smp.dll")
                if (vm_root / name).is_file()
            ),
            None,
        )
        if vm is None:
            raise ValueError("BEAM emulator binary is unavailable")
        if any(not isinstance(item, str) or not item for item in data["files"]):
            raise ValueError("invalid standard library paths")
        launcher = Path((launcher_command or command)[0])
        paths = (launcher, vm, *(Path(item) for item in data["files"]))
        if launcher_command is not None:
            paths += (Path(command[0]),)
        # Elixir launches BEAM through a shell script (or a Windows batch file).
        # Bind that interpreter as well; native dependency inspection must start
        # from BEAM and the interpreter, never treat the script as a native image.
        if os.name == "nt" and launcher.suffix.lower() in {".bat", ".cmd"}:
            interpreter = shutil.which("cmd.exe", path=environment.get("PATH"))
            if interpreter is None:
                raise ValueError("batch interpreter is unavailable")
            paths += (Path(interpreter),)
        elif launcher.read_bytes().startswith(b"#!/bin/sh\n"):
            paths += (Path(launcher.read_bytes().splitlines()[0][2:].decode()),)
        bindings = tuple(
            (str(path.resolve(strict=True)), executable_file_digest(path))
            for path in paths
        )
        return data["version"], tuple(map(int, match.groups())), data["otp"], bindings
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise BuildError(
            "builder.elixir_version_unsupported",
            "Elixir probe requires Elixir 1.18+, Erlang/OTP 27+, built-in JSON "
            "and a readable BEAM installation",
        ) from exc


@dataclass(frozen=True, slots=True)
class ElixirToolchain:
    """Launcher, BEAM and core module bytes bound to one Elixir/OTP version."""

    command: tuple[str, ...]
    version: str
    version_info: tuple[int, int, int]
    otp_version: str
    file_bindings: tuple[tuple[str, str], ...]
    launcher_command: tuple[str, ...] | None = None

    @property
    def native_dependency_commands(self) -> tuple[tuple[str, ...], ...]:
        return ((self.file_bindings[1][0],),) + tuple(
            (path,) for path, _ in self.file_bindings[5:]
        )

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        value = {
            "command": list(self.command),
            "version": self.version,
            "version_info": list(self.version_info),
            "otp_version": self.otp_version,
            "file_bindings": [list(item) for item in self.file_bindings],
        }
        if self.launcher_command is not None:
            value["launcher_command"] = list(self.launcher_command)
        return value

    def require_unchanged(self, environment: Mapping[str, str] | None = None) -> None:
        configured = dict(os.environ if environment is None else environment)
        try:
            observed = _probe(
                self.command,
                configured,
                DEFAULT_ELIXIR_VERSION_TIMEOUT_SECONDS,
                self.launcher_command,
            )
        except BuildError as exc:
            raise BuildError(
                "builder.elixir_toolchain_changed",
                "Elixir/OTP installation became unavailable after selection",
            ) from exc
        if observed != (
            self.version,
            self.version_info,
            self.otp_version,
            self.file_bindings,
        ):
            raise BuildError(
                "builder.elixir_toolchain_changed",
                "Elixir/OTP installation changed after selection",
            )


def discover_elixir_toolchain(
    environment: Mapping[str, str] | None = None,
    *,
    pinned_command: str | Sequence[str] | None = None,
    minimum_version: tuple[int, ...] = DEFAULT_ELIXIR_MINIMUM_VERSION,
    required_version: tuple[int, ...] | None = None,
    timeout_seconds: float = DEFAULT_ELIXIR_VERSION_TIMEOUT_SECONDS,
) -> ElixirToolchain:
    """Honor exact typed or ELIXIR pins; never fall back after a bad explicit pin."""

    _constraint(minimum_version)
    _constraint(required_version)
    configured = dict(os.environ if environment is None else environment)
    raw = (
        pinned_command
        if pinned_command is not None
        else configured.get("ELIXIR", "elixir")
    )
    try:
        command = (
            tuple(shlex.split(raw, posix=os.name != "nt"))
            if isinstance(raw, str)
            else tuple(raw)
        )
    except ValueError as exc:
        raise BuildError("builder.elixir_toolchain", "invalid Elixir command") from exc
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise BuildError("builder.elixir_toolchain", "invalid Elixir command")
    found = shutil.which(command[0], path=configured.get("PATH"))
    if found is None:
        raise BuildError(
            "builder.elixir_toolchain_unavailable",
            "Elixir command was not found on PATH",
        )
    command = (os.path.abspath(found), *command[1:])
    launcher_command = None
    if os.name == "nt" and Path(command[0]).suffix.lower() in {".bat", ".cmd"}:
        # cmd.exe cannot faithfully transport multiline code or arbitrary JSON
        # through elixir.bat. Invoke its native Erlang entry point directly for
        # framework phases, preserving the selected Elixir library installation.
        erl = shutil.which("erl.exe", path=configured.get("PATH"))
        if erl is None:
            raise BuildError(
                "builder.elixir_toolchain_unavailable", "Erlang launcher is unavailable"
            )
        launcher_command = command
        library = Path(command[0]).parent.parent / "lib"
        command = (
            os.path.abspath(erl),
            *shlex.split(configured.get("ELIXIR_ERL_OPTIONS", ""), posix=False),
            "-noshell",
            "-elixir_root",
            str(library),
            "-pa",
            str(library / "elixir" / "ebin"),
            "-s",
            "elixir",
            "start_cli",
            "-extra",
            *launcher_command[1:],
        )
    version, parts, otp, bindings = _probe(
        command, configured, timeout_seconds, launcher_command
    )
    if parts[: len(minimum_version)] < minimum_version or (
        required_version is not None
        and parts[: len(required_version)] != required_version
    ):
        raise BuildError(
            "builder.elixir_version_unsupported",
            "Elixir does not satisfy the selected version constraints",
        )
    toolchain = ElixirToolchain(
        command, version, parts, otp, bindings, launcher_command
    )
    toolchain.require_unchanged(configured)
    return toolchain
