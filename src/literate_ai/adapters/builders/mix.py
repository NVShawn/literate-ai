"""Mix discovery bound to one Elixir installation, without project evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts import ContentIdentity, canonical_identity

from ._process import run_bounded_process
from .elixir import DEFAULT_ELIXIR_VERSION_TIMEOUT_SECONDS, ElixirToolchain
from .python import BuildError, executable_file_digest

_PROBE = """Application.load(:mix)
files = Enum.map([Mix.CLI, Mix.Project, Mix.Dep.Lock], fn module ->
  Code.ensure_loaded!(module)
  List.to_string(:code.which(module))
end)
IO.puts(JSON.encode!(%{version: List.to_string(Application.spec(:mix, :vsn)),
  files: files}))
"""
_MAX_MIX_FILES = 1024
_MAX_MIX_BYTES = 64 * 1024 * 1024


def _observe(
    elixir: ElixirToolchain, environment: Mapping[str, str]
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Observe shipped Mix code; never invoke a project or install an archive."""

    elixir.require_unchanged(environment)
    installation = Path(elixir.file_bindings[0][0]).parent.parent
    script = installation / "bin" / "mix"
    ebin = (installation / "lib" / "mix" / "ebin").resolve(strict=True)
    result = run_bounded_process(
        (*elixir.command, "-e", _PROBE),
        cwd=None,
        environment=environment,
        timeout_seconds=DEFAULT_ELIXIR_VERSION_TIMEOUT_SECONDS,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
        error_prefix="builder.mix_version",
    )
    try:
        data = json.loads(result.stdout)
        if (
            result.returncode
            or not isinstance(data, dict)
            or set(data) != {"version", "files"}
            or data["version"] != elixir.version
            or not isinstance(data["files"], list)
            or len(data["files"]) != 3
        ):
            raise ValueError("Mix must match the selected Elixir version")
        expected = tuple(
            ebin / name
            for name in (
                "Elixir.Mix.CLI.beam",
                "Elixir.Mix.Project.beam",
                "Elixir.Mix.Dep.Lock.beam",
            )
        )
        observed = tuple(Path(name).resolve(strict=True) for name in data["files"])
        if observed != expected:
            raise ValueError("Mix modules belong to another installation")
        # Bind the complete shipped application, including compiler/dependency
        # tasks, rather than trusting only the three modules used by the probe.
        paths = (script, *sorted(ebin.iterdir()))
        if (
            len(paths) > _MAX_MIX_FILES
            or any(path.is_symlink() or not path.is_file() for path in paths)
            or sum(path.stat().st_size for path in paths) > _MAX_MIX_BYTES
            or any(path.suffix not in {".beam", ".app"} for path in paths[1:])
        ):
            raise ValueError("Mix installation is not a bounded regular payload")
        bindings = tuple(
            (str(path.resolve(strict=True)), executable_file_digest(path))
            for path in paths
        )
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise BuildError(
            "builder.mix_toolchain_unavailable",
            "Mix requires the complete application from the selected "
            "Elixir installation",
        ) from exc
    finally:
        elixir.require_unchanged(environment)
    return data["version"], bindings


@dataclass(frozen=True, slots=True)
class MixToolchain:
    """The selected native Elixir command plus its shipped Mix script and modules."""

    elixir: ElixirToolchain
    version: str
    file_bindings: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.elixir, ElixirToolchain)
            or self.version != self.elixir.version
            or not isinstance(self.file_bindings, tuple)
            or not 4 <= len(self.file_bindings) <= _MAX_MIX_FILES
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not Path(item[0]).is_absolute()
                or not isinstance(item[1], str)
                for item in self.file_bindings
            )
            or len({path for path, _ in self.file_bindings}) != len(self.file_bindings)
        ):
            raise ValueError("Mix requires exact installation file bindings")
        for _, digest in self.file_bindings:
            ContentIdentity.parse_uri(digest)

    @property
    def command(self) -> tuple[str, ...]:
        # Windows uses the already-bound native erl.exe command. No cmd.exe
        # argument re-parsing or ambient `elixir` shebang lookup is involved.
        return (*self.elixir.command, self.file_bindings[0][0])

    @property
    def native_dependency_commands(self) -> tuple[tuple[str, ...], ...]:
        return self.elixir.native_dependency_commands

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/mix-toolchain@1",
            "elixir_toolchain_identity": self.elixir.identity,
            "command": list(self.command),
            "version": self.version,
            "file_bindings": [list(item) for item in self.file_bindings],
        }

    def require_unchanged(self, environment: Mapping[str, str] | None = None) -> None:
        configured = dict(os.environ if environment is None else environment)
        try:
            observed = _observe(self.elixir, configured)
        except (BuildError, OSError) as exc:
            raise BuildError(
                "builder.mix_toolchain_changed",
                "Mix or its selected Elixir installation became unavailable",
            ) from exc
        if observed != (self.version, self.file_bindings):
            raise BuildError(
                "builder.mix_toolchain_changed",
                "Mix payload changed after selection",
            )


def discover_mix_toolchain(
    elixir: ElixirToolchain,
    environment: Mapping[str, str] | None = None,
    *,
    pinned_command: str | Sequence[str] | None = None,
) -> MixToolchain:
    """Select Mix from one Elixir installation; reject competing explicit pins."""

    if not isinstance(elixir, ElixirToolchain):
        raise TypeError("Mix discovery requires an observed ElixirToolchain")
    configured = dict(os.environ if environment is None else environment)
    expected = Path(elixir.file_bindings[0][0]).parent / "mix"
    raw = pinned_command if pinned_command is not None else configured.get("MIX")
    if raw is not None:
        values = (raw,) if isinstance(raw, str) else tuple(raw)
        try:
            if (
                len(values) != 1
                or not isinstance(values[0], str)
                or not values[0]
                or Path(values[0]).resolve(strict=True) != expected.resolve(strict=True)
            ):
                raise ValueError("Mix pin differs from selected Elixir installation")
        except (ValueError, OSError) as exc:
            raise BuildError(
                "builder.mix_toolchain_unavailable",
                "explicit Mix pin must name the selected installation's Mix script",
            ) from exc
    try:
        version, files = _observe(elixir, configured)
    except OSError as exc:
        raise BuildError(
            "builder.mix_toolchain_unavailable",
            "Mix is absent from the selected Elixir installation",
        ) from exc
    result = MixToolchain(elixir, version, files)
    result.require_unchanged(configured)
    return result
