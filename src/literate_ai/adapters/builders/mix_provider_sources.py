"""Compile published package source under a trusted consumer lifecycle owner."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory
from literate_ai.contracts import ContentIdentity, LibraryImportSurface
from literate_ai.contracts.elixir_libraries import elixir_namespace

from ._process import run_bounded_process
from .hex import HexToolchain
from .mix_project import (
    _INVENTORY_PROBE,
    MixProviderLibrary,
    _files,
    _provider_code_paths,
)
from .python import BuildError, canonical_tree_digest

_COMPILE = """data = JSON.decode!(File.read!(hd(System.argv())))
normalize = fn path ->
  expanded = Path.expand(path)
  if match?({:win32, _}, :os.type()), do: String.downcase(expanded), else: expanded
end
files = data["files"]
origins = Enum.map(files, normalize)
namespace = data["namespace"]
seen = make_ref()
each_module = fn file, module, _binary ->
  name = Atom.to_string(module)
  unless name == namespace or String.starts_with?(name, namespace <> "."),
    do: raise("provider module escapes namespace")
  unless normalize.(file) in origins,
    do: raise("provider compiled outside retained source closure")
  loaded = Process.get(seen, MapSet.new())
  if MapSet.member?(loaded, module), do: raise("duplicate provider source module")
  Process.put(seen, MapSet.put(loaded, module))
end
modules = case Kernel.ParallelCompiler.compile_to_path(files, data["ebin"],
    warnings_as_errors: true, each_module: each_module) do
  {:ok, modules, []} -> modules
  _ -> raise("provider source compilation failed")
end
Process.delete(seen)
IO.puts(JSON.encode!(Enum.map(modules, &Atom.to_string/1)))
"""


@dataclass(frozen=True)
class MixProviderSourceLibrary:
    """Exact retained package source, bound by its published artifact owner."""

    artifact_identity: ContentIdentity
    import_surface: LibraryImportSurface
    source_tree: Path
    tree_digest: str

    def __post_init__(self):
        if (
            not isinstance(self.artifact_identity, ContentIdentity)
            or not isinstance(self.import_surface, LibraryImportSurface)
            or self.import_surface.language != "elixir"
            or not isinstance(self.source_tree, Path)
            or not self.source_tree.is_absolute()
            or ".." in self.source_tree.parts
        ):
            raise TypeError("Mix provider source requires exact Elixir authority")
        ContentIdentity.parse_uri(self.tree_digest)

    def require_unchanged(self):
        try:
            require_safe_directory(self.source_tree)
        except (OSError, UnsafeFilesystemPathError) as exc:
            raise BuildError(
                "builder.mix_provider_invalid", "Provider source directory is unsafe"
            ) from exc
        files = _files(self.source_tree)
        if not any(Path(name).suffix == ".ex" for name in files):
            raise BuildError(
                "builder.mix_provider_invalid", "Provider has no module sources"
            )
        if canonical_tree_digest(self.source_tree) != self.tree_digest:
            raise BuildError(
                "builder.mix_provider_changed", "Selected provider source changed"
            )

    def to_dict(self):
        return {
            "artifact_identity": self.artifact_identity.uri,
            "import_surface": self.import_surface.to_dict(),
            "source_tree_digest": self.tree_digest,
        }


def _compile_mix_provider_source(
    source: MixProviderSourceLibrary,
    *,
    toolchain: HexToolchain,
    workspace: Path,
    require_authority: Callable[[], None],
    timeout_seconds: float = 900,
    dependencies: tuple[MixProviderLibrary, ...] = (),
) -> MixProviderLibrary:
    """Owner-only producer hook; data records never authorize native execution.

    The Standard owner checks its issued consumer grant, exact published exports
    and direct-interface bindings before and after this process. No second grant
    or historical producer grant is manufactured here.
    """
    if not isinstance(source, MixProviderSourceLibrary):
        raise TypeError("Provider compilation requires a typed source selection")
    if not isinstance(dependencies, tuple) or any(
        not isinstance(item, MixProviderLibrary) for item in dependencies
    ):
        raise TypeError("Provider compilation requires typed compiled dependencies")
    require_authority()
    source.require_unchanged()
    workspace.mkdir(parents=True, exist_ok=False)
    copied = workspace / "source"
    shutil.copytree(source.source_tree, copied)
    ebin = workspace / "ebin"
    ebin.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("MIX_", "HEX_"))
        and key.upper()
        not in {"ERL_LIBS", "ERL_FLAGS", "ERL_AFLAGS", "ELIXIR_ERL_OPTIONS"}
    }

    def guard():
        require_authority()
        source.require_unchanged()
        if canonical_tree_digest(copied) != source.tree_digest:
            raise BuildError(
                "builder.mix_provider_changed", "Copied provider source changed"
            )
        toolchain.require_unchanged(environment)
        for dependency in dependencies:
            dependency.require_unchanged()

    manifest = workspace / "input.json"
    document = {
        **source.to_dict(),
        "namespace": "Elixir." + elixir_namespace(source.import_surface.package),
        "files": [str(path) for path in sorted(copied.rglob("*.ex"))],
        "ebin": str(ebin),
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")
    pinned_manifest = manifest.read_bytes()
    if dependencies:
        dependency_paths = _provider_code_paths(dependencies)
        inventory = workspace / "dependency-input.json"
        inventory.write_text(
            json.dumps(
                {
                    "modules": [
                        str(path)
                        for ebin in dependency_paths
                        for path in sorted(ebin.glob("*.beam"))
                    ],
                }
            ),
            encoding="utf-8",
        )
        pinned_inventory = inventory.read_bytes()
        guard()
        try:
            observed = run_bounded_process(
                (
                    *toolchain.mix.elixir.command,
                    "-e",
                    _INVENTORY_PROBE,
                    "--",
                    str(inventory),
                ),
                cwd=workspace,
                environment=environment,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=4 * 1024 * 1024,
                stderr_limit_bytes=4 * 1024 * 1024,
                error_prefix="builder.mix_provider",
            )
        finally:
            guard()
            if inventory.read_bytes() != pinned_inventory:
                raise BuildError(
                    "builder.mix_provider_changed",
                    "Provider dependency probe inputs changed",
                )
        (workspace / "dependency-inventory.stdout").write_bytes(observed.stdout)
        (workspace / "dependency-inventory.stderr").write_bytes(observed.stderr)
        if observed.returncode:
            raise BuildError(
                "builder.mix_provider_invalid", "Provider dependency collision"
            )
    guard()
    try:
        process = run_bounded_process(
            (
                *toolchain.mix.elixir.command,
                *(
                    token
                    for ebin in _provider_code_paths(dependencies)
                    for token in ("-pa", str(ebin))
                ),
                "-e",
                _COMPILE,
                "--",
                str(manifest),
            ),
            cwd=workspace,
            environment=environment,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=4 * 1024 * 1024,
            stderr_limit_bytes=4 * 1024 * 1024,
            error_prefix="builder.mix_provider",
        )
    finally:
        guard()
        if manifest.read_bytes() != pinned_manifest:
            raise BuildError(
                "builder.mix_provider_changed", "Provider compile inputs changed"
            )
    (workspace / "compile.stdout").write_bytes(process.stdout)
    (workspace / "compile.stderr").write_bytes(process.stderr)
    if process.returncode:
        raise BuildError(
            "builder.mix_provider_compile_failed",
            process.stderr.decode("utf-8", errors="replace"),
        )
    compiled = MixProviderLibrary(
        source.artifact_identity,
        source.import_surface,
        ebin,
        canonical_tree_digest(ebin),
    )
    compiled.require_unchanged()
    inventory = workspace / "module-input.json"
    paths = [str(path) for path in sorted(ebin.glob("*.beam"))]
    inventory.write_text(json.dumps({"modules": paths}), encoding="utf-8")
    guard()
    try:
        observed = run_bounded_process(
            (
                *toolchain.mix.elixir.command,
                "-e",
                _INVENTORY_PROBE,
                "--",
                str(inventory),
            ),
            cwd=workspace,
            environment=environment,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=4 * 1024 * 1024,
            stderr_limit_bytes=4 * 1024 * 1024,
            error_prefix="builder.mix_provider",
        )
    finally:
        guard()
        compiled.require_unchanged()
    (workspace / "module-inventory.stdout").write_bytes(observed.stdout)
    (workspace / "module-inventory.stderr").write_bytes(observed.stderr)
    if observed.returncode:
        raise BuildError(
            "builder.mix_provider_invalid", "Compiled provider module collision"
        )
    modules = json.loads(observed.stdout)
    namespace = document["namespace"]
    if (
        not isinstance(modules, list)
        or [item.get("path") for item in modules if isinstance(item, dict)] != paths
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("module"), str)
            or not (
                item["module"] == namespace
                or item["module"].startswith(namespace + ".")
            )
            for item in modules
        )
    ):
        raise BuildError(
            "builder.mix_provider_invalid", "Compiled provider escapes source namespace"
        )
    (workspace / "compiled.json").write_text(
        json.dumps(compiled.to_dict()), encoding="utf-8"
    )
    guard()
    return compiled
