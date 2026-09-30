"""Stdlib-only C++ object compilation and linking for native command drivers."""

from __future__ import annotations

import ast
import base64
import subprocess
import zlib
from pathlib import Path


def compiler_driver_source(
    tail: str = "", *, filename: str = "<literate-ai-native-build>"
) -> str:
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "compile_cpp"
    )
    function.body = function.body[1:]  # The emitted helper needs no docstring.
    source = (
        "import subprocess\nfrom pathlib import Path\n"
        + ast.unparse(function)
        + "\n"
        + tail
    )
    encoded = base64.urlsafe_b64encode(zlib.compress(source.encode(), 9)).decode(
        "ascii"
    )
    return (
        "import base64,zlib;exec(compile(zlib.decompress(base64.urlsafe_b64decode("
        + repr(encoded)
        + ")), "
        + repr(filename)
        + ", 'exec'))"
    )


def compile_cpp(
    compiler: list[str],
    sources: list[str],
    include_root: Path,
    object_root: Path,
    output: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """Cache individual translations; link all objects with the locked compiler."""
    msvc = Path(compiler[0]).name.lower() in {
        "cl",
        "cl.exe",
        "clang-cl",
        "clang-cl.exe",
    }
    wrapper = environment.get("LITAI_COMPILER_CACHE_TOOL")
    if wrapper and not Path(wrapper).is_absolute():
        raise ValueError("compiler-cache tool must be absolute")
    prefix = [wrapper] if wrapper else []
    objects = []
    stdout, stderr = bytearray(), bytearray()
    object_root.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(sources):
        obj = object_root / (f"unit-{index}" + (".obj" if msvc else ".o"))
        arguments = (
            [
                "/nologo",
                "/std:c++17",
                "/EHsc",
                "/I" + str(include_root),
                "/c",
                source,
                "/Fo" + str(obj),
            ]
            if msvc
            else [
                "-std=c++17",
                "-O2",
                "-I",
                str(include_root),
                "-c",
                source,
                "-o",
                str(obj),
            ]
        )
        result = subprocess.run(
            [*prefix, *compiler, *arguments],
            capture_output=True,
            env=environment,
            check=False,
        )
        stdout.extend(result.stdout)
        stderr.extend(result.stderr)
        if result.returncode:
            return subprocess.CompletedProcess(
                result.args, result.returncode, bytes(stdout), bytes(stderr)
            )
        objects.append(str(obj))
    arguments = (
        ["/nologo", *objects, "/Fe" + str(output)]
        if msvc
        else [*objects, "-o", str(output)]
    )
    result = subprocess.run(
        [*compiler, *arguments], capture_output=True, env=environment, check=False
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        bytes(stdout) + result.stdout,
        bytes(stderr) + result.stderr,
    )
