"""Explicit Hex plugin custody for an already observed Mix installation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts import ContentIdentity, canonical_identity

from ._process import run_bounded_process
from .elixir import DEFAULT_ELIXIR_VERSION_TIMEOUT_SECONDS
from .mix import MixToolchain
from .python import BuildError, executable_file_digest

_MODULES = (
    "Hex",
    "Hex.Application",
    "Hex.State",
    "Hex.Registry.Server",
    "Hex.SCM",
    "Hex.Solver",
    "Hex.RemoteConverger",
    "Hex.HTTP",
    "Hex.Tar",
    "Mix.Tasks.Hex.Build",
)
_MAX_FILES = 2048
_MAX_BYTES = 64 * 1024 * 1024
_PROBE = """Application.load(:hex)
files = Enum.map([Hex, Hex.Application, Hex.State,
  Hex.Registry.Server, Hex.SCM, Hex.Solver, Hex.RemoteConverger,
  Hex.HTTP, Hex.Tar, Mix.Tasks.Hex.Build], fn module ->
  Code.ensure_loaded!(module)
  List.to_string(:code.which(module))
end)
version = Hex.version()
if List.to_string(Application.spec(:hex, :vsn)) != version,
  do: raise("Hex application and module versions differ")
IO.puts(JSON.encode!(%{version: version, files: files}))
"""


def _payload(ebin: Path) -> tuple[tuple[str, str], ...]:
    if ebin.is_symlink() or not ebin.is_dir():
        raise ValueError("Hex requires an explicit regular ebin directory")
    paths = tuple(sorted(ebin.iterdir()))
    if (
        not paths
        or len(paths) > _MAX_FILES
        or any(
            path.is_symlink()
            or not path.is_file()
            or path.suffix not in {".beam", ".app"}
            for path in paths
        )
        or sum(path.stat().st_size for path in paths) > _MAX_BYTES
        or not (ebin / "hex.app").is_file()
        or any(not (ebin / f"Elixir.{name}.beam").is_file() for name in _MODULES)
    ):
        raise ValueError("Hex requires a bounded complete application payload")
    return tuple(
        (str(path.resolve(strict=True)), executable_file_digest(path)) for path in paths
    )


def _observe(
    mix: MixToolchain, ebin: Path, environment: Mapping[str, str]
) -> tuple[str, tuple[tuple[str, str], ...]]:
    mix.require_unchanged(environment)
    try:
        before = _payload(ebin)
        result = run_bounded_process(
            (*mix.elixir.command, "-pa", str(ebin), "-e", _PROBE),
            cwd=None,
            environment=environment,
            timeout_seconds=DEFAULT_ELIXIR_VERSION_TIMEOUT_SECONDS,
            stdout_limit_bytes=1024 * 1024,
            stderr_limit_bytes=1024 * 1024,
            error_prefix="builder.hex_version",
        )
        data = json.loads(result.stdout)
        if (
            result.returncode
            or not isinstance(data, dict)
            or set(data) != {"version", "files"}
            or not isinstance(data["version"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", data["version"])
            or not isinstance(data["files"], list)
            or len(data["files"]) != len(_MODULES)
        ):
            raise ValueError("Hex critical modules must load on the selected OTP")
        expected = tuple(ebin / f"Elixir.{name}.beam" for name in _MODULES)
        observed = tuple(Path(name).resolve(strict=True) for name in data["files"])
        if observed != expected or before != _payload(ebin):
            raise ValueError("Hex module origin or payload changed during observation")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise BuildError(
            "builder.hex_toolchain_unavailable",
            "Hex requires a complete explicit payload whose critical modules "
            "load from that payload on the selected Elixir/OTP installation",
        ) from exc
    finally:
        mix.require_unchanged(environment)
    return data["version"], before


@dataclass(frozen=True, slots=True)
class HexToolchain:
    """Observed plugin bytes; discovery never starts Hex or evaluates a project."""

    mix: MixToolchain
    ebin: str
    version: str
    file_bindings: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.mix, MixToolchain)
            or not isinstance(self.ebin, str)
            or not Path(self.ebin).is_absolute()
            or not isinstance(self.version, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version)
            or not isinstance(self.file_bindings, tuple)
            or not len(_MODULES) + 1 <= len(self.file_bindings) <= _MAX_FILES
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or Path(item[0]).parent != Path(self.ebin)
                or not isinstance(item[1], str)
                for item in self.file_bindings
            )
            or len({path for path, _ in self.file_bindings}) != len(self.file_bindings)
        ):
            raise ValueError("Hex requires exact plugin file bindings")
        for _, digest in self.file_bindings:
            ContentIdentity.parse_uri(digest)

    @property
    def command(self) -> tuple[str, ...]:
        return (
            *self.mix.elixir.command,
            "-pa",
            self.ebin,
            self.mix.file_bindings[0][0],
        )

    @property
    def native_dependency_commands(self) -> tuple[tuple[str, ...], ...]:
        return self.mix.native_dependency_commands

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/hex-toolchain@1",
            "mix_toolchain_identity": self.mix.identity,
            "command": list(self.command),
            "version": self.version,
            "file_bindings": [list(item) for item in self.file_bindings],
        }

    def require_unchanged(self, environment: Mapping[str, str] | None = None) -> None:
        configured = dict(os.environ if environment is None else environment)
        try:
            observed = _observe(self.mix, Path(self.ebin), configured)
        except BuildError as exc:
            raise BuildError(
                "builder.hex_toolchain_changed",
                "Hex or its selected Mix/Elixir installation became unavailable",
            ) from exc
        if observed != (self.version, self.file_bindings):
            raise BuildError(
                "builder.hex_toolchain_changed", "Hex payload changed after selection"
            )


def discover_hex_toolchain(
    mix: MixToolchain,
    *,
    ebin: Path,
    environment: Mapping[str, str] | None = None,
) -> HexToolchain:
    """Observe an explicitly staged plugin; never search ambient Hex archives."""

    if not isinstance(mix, MixToolchain):
        raise TypeError("Hex discovery requires an observed MixToolchain")
    configured = dict(os.environ if environment is None else environment)
    try:
        if ebin.is_symlink():
            raise ValueError("Hex plugin directory must not be a symlink")
        selected = ebin.resolve(strict=True)
    except (ValueError, OSError) as exc:
        raise BuildError(
            "builder.hex_toolchain_unavailable", "explicit Hex payload is unavailable"
        ) from exc
    version, bindings = _observe(mix, selected, configured)
    return HexToolchain(mix, str(selected), version, bindings)
