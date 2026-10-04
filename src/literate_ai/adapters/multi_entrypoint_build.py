"""Shell-free host driver for one Component build with many exact outputs.

The command contract passes only immutable JSON and path arguments.  This module
turns those declarations into local compiler invocations; it intentionally owns
no Component policy and never discovers tools or output names on its own.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path, PurePosixPath

_TREE_STRATEGIES = frozenset(
    {"python-tree", "javascript-tree", "typescript-tree", "elixir-tree"}
)


def standalone_driver_source() -> str:
    """Embed exact stdlib-only driver bytes for the selected host Python.

    The host interpreter needs no installed framework import. This payload is
    part of the locked command identity.
    """

    encoded = base64.urlsafe_b64encode(
        zlib.compress(Path(__file__).read_bytes(), level=9)
    ).decode("ascii")
    return (
        "import base64,zlib;"
        "exec(compile(zlib.decompress(base64.urlsafe_b64decode("
        + repr(encoded)
        + ')),"<literate-ai-multi-entrypoint-build>","exec"))'
    )


def _decode_environment(encoded: str) -> dict[str, str]:
    raw = zlib.decompress(base64.urlsafe_b64decode(encoded)).decode("utf-8")
    pairs = json.loads(raw)
    if not isinstance(pairs, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or any(not isinstance(value, str) for value in pair)
        for pair in pairs
    ):
        raise ValueError("compiler environment must be a JSON string-pair list")
    environment = dict(os.environ)
    if os.name == "nt":
        overridden = {name.casefold() for name, _value in pairs}
        environment = {
            name: value
            for name, value in environment.items()
            if name.casefold() not in overridden
        }
    environment.update(dict(pairs))
    return environment


def _relative_path(root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str):
        raise ValueError(f"{label} must be a string")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ValueError(f"{label} must be a contained relative POSIX path")
    path = root.joinpath(*pure.parts)
    if root != path and root not in path.parents:
        raise ValueError(f"{label} escaped its root")
    return path


def _descriptors(value: str, source_root: Path, artifact_root: Path):
    raw = json.loads(zlib.decompress(base64.urlsafe_b64decode(value)).decode("utf-8"))
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError("multi-entrypoint build requires at least two outputs")
    result: list[tuple[Path, Path]] = []
    output_names: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"source", "export_id"}:
            raise ValueError(f"output descriptor {index} is invalid")
        source = _relative_path(
            source_root, item["source"], label=f"output descriptor {index} source"
        )
        output = _relative_path(
            artifact_root,
            item["export_id"],
            label=f"output descriptor {index} export",
        )
        if output.parent != artifact_root or output.name in output_names:
            raise ValueError("output IDs must be unique direct artifact children")
        if not source.is_file():
            raise ValueError(f"declared entrypoint source is absent: {item['source']}")
        output_names.add(output.name)
        result.append((source, output))
    return tuple(result)


def _run(argv: list[str], *, environment: dict[str, str]) -> None:
    completed = subprocess.run(argv, capture_output=True, env=environment, check=False)
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def _check_tree(
    strategy: str,
    compiler: list[str],
    source_root: Path,
    *,
    environment: dict[str, str],
) -> None:
    if strategy == "elixir-tree":
        files = sorted(
            path
            for path in source_root.rglob("*")
            if path.suffix in {".ex", ".exs"} and path.is_file()
        )
        _run(
            [
                *compiler,
                "-e",
                "Enum.each(System.argv(), fn path -> "
                "Code.string_to_quoted!(File.read!(path), file: path) end)",
                "--",
                *(str(path) for path in files),
            ],
            environment=environment,
        )
        return
    suffix = {
        "python-tree": ".py",
        "javascript-tree": ".js",
        "typescript-tree": ".ts",
    }[strategy]
    files = sorted(path for path in source_root.rglob(f"*{suffix}") if path.is_file())
    if strategy == "python-tree":
        for path in files:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
    elif strategy == "javascript-tree":
        for path in files:
            _run([*compiler, "--check", str(path)], environment=environment)


def build_many(
    strategy: str,
    compiler: list[str],
    compiler_environment: str,
    source_root: Path,
    object_root: Path,
    artifact_root: Path,
    primary_export: Path,
    descriptor_json: str,
) -> None:
    """Realize all declared outputs or fail before publishing any artifact."""

    source_root = source_root.resolve(strict=True)
    artifact_root = artifact_root.resolve(strict=True)
    primary_export = primary_export.resolve(strict=False)
    object_root.mkdir(parents=True, exist_ok=True)
    descriptors = _descriptors(descriptor_json, source_root, artifact_root)
    if descriptors[0][1] != primary_export:
        raise ValueError("primary export path does not match the first declaration")
    environment = _decode_environment(compiler_environment)
    if strategy in _TREE_STRATEGIES:
        _check_tree(strategy, compiler, source_root, environment=environment)
        for _source, output in descriptors:
            if output.exists() or output.is_symlink():
                if output.is_dir() and not output.is_symlink():
                    shutil.rmtree(output)
                else:
                    output.unlink()
            shutil.copytree(source_root, output)
        return

    entrypoint_sources = {source for source, _output in descriptors}
    cpp_root = source_root / "source"
    shared_cpp = (
        sorted(
            str(path)
            for path in cpp_root.rglob("*.cpp")
            if path.is_file() and path not in entrypoint_sources
        )
        if strategy == "cpp-executable"
        else []
    )
    compiler_name = Path(compiler[0]).name.lower()
    for source, output in descriptors:
        output.parent.mkdir(parents=True, exist_ok=True)
        if strategy == "zig-executable":
            argv = [*compiler, "build-exe", str(source), "-femit-bin=" + str(output)]
        elif strategy == "rust-executable":
            argv = [*compiler, "--edition=2021", str(source), "-o", str(output)]
        elif strategy == "go-executable":
            argv = [*compiler, "build", "-o", str(output), str(source)]
        elif strategy == "swift-executable":
            argv = [*compiler, str(source), "-o", str(output)]
        elif strategy == "cpp-executable" and compiler_name in {
            "cl",
            "cl.exe",
            "clang-cl",
            "clang-cl.exe",
        }:
            argv = [
                *compiler,
                "/nologo",
                "/std:c++17",
                "/EHsc",
                "/I" + str(cpp_root),
                str(source),
                *shared_cpp,
                "/Fe" + str(output),
            ]
        elif strategy == "cpp-executable":
            argv = [
                *compiler,
                "-std=c++17",
                "-O2",
                "-I",
                str(cpp_root),
                str(source),
                *shared_cpp,
                "-o",
                str(output),
            ]
        else:
            raise ValueError(f"unsupported multi-entrypoint build strategy: {strategy}")
        _run(argv, environment=environment)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 8:
        raise ValueError("multi-entrypoint build driver received invalid arguments")
    strategy, compiler_json, compiler_environment, *paths, descriptor_json = arguments
    compiler = json.loads(compiler_json)
    if (
        not isinstance(compiler, list)
        or not compiler
        or any(not isinstance(item, str) or not item for item in compiler)
    ):
        raise ValueError("compiler command must be a non-empty JSON string list")
    source_root, object_root, artifact_root, primary_export = map(Path, paths)
    build_many(
        strategy,
        compiler,
        compiler_environment,
        source_root,
        object_root,
        artifact_root,
        primary_export,
        descriptor_json,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
