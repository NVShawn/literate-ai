"""Compile verifier-owned C++ against the exact materialized native product."""

from __future__ import annotations

import inspect
import os
from pathlib import Path

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.contracts import CppLibraryLayout


class CppAcceptanceError(ValueError):
    pass


def cpp_compile_command(compiler, export, layout, harness, executable):
    """Portable argv construction shared by lifecycle and independent verifiers."""
    includes = export / "include"
    libraries = tuple(str(export / name) for name in layout["link_files"])
    runtime_dirs = tuple(
        sorted({str((export / name).parent) for name in layout["runtime_files"]})
    )
    msvc = Path(compiler[0]).name.lower() in {
        "cl",
        "cl.exe",
        "clang-cl",
        "clang-cl.exe",
    }
    if msvc:
        command = (
            *compiler,
            "/nologo",
            "/std:c++17",
            "/EHsc",
            "/MD",
            "/I" + str(includes),
            str(harness),
            *libraries,
            "/Fe:" + str(executable),
            "/Fo:" + str(executable.with_suffix(".obj")),
        )
    else:
        runtime_flags = tuple(
            token
            for directory in runtime_dirs
            for token in ("-Xlinker", "-rpath", "-Xlinker", directory)
        )
        command = (
            *compiler,
            "-std=c++17",
            "-I" + str(includes),
            str(harness),
            *libraries,
            *runtime_flags,
            "-o",
            str(executable),
        )
    return command, runtime_dirs


def cpp_compile_driver_source() -> str:
    return "from pathlib import Path\n" + inspect.getsource(cpp_compile_command)


def compile_cpp_verifier(
    *,
    compiler: tuple[str, ...],
    environment: dict[str, str],
    export: Path,
    layout: CppLibraryLayout,
    harness: Path,
    executable: Path,
) -> dict[str, str]:
    """Compile in verifier custody and return its declared runtime search paths.

    The caller owns execution authorization, toolchain identity guards, artifact
    identity checks and the acceptance case protocol. This function neither accepts
    a candidate nor admits producer source into verifier custody.
    """
    if not isinstance(layout, CppLibraryLayout):
        raise CppAcceptanceError("C++ verifier requires a typed product layout")
    if export.is_symlink() or not export.is_dir():
        raise CppAcceptanceError("C++ verifier requires a regular export directory")
    actual = set()
    for path in export.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise CppAcceptanceError("C++ export contains unsafe file nodes")
        if path.is_file():
            actual.add(path.relative_to(export).as_posix())
    if actual != set(layout.files):
        raise CppAcceptanceError("C++ export differs from its declared file closure")
    if not compiler or not Path(compiler[0]).is_absolute():
        raise CppAcceptanceError("C++ verifier requires an exact compiler executable")
    if executable.exists() or executable.is_symlink():
        raise CppAcceptanceError("C++ verifier output must be fresh")
    command, runtime_dirs = cpp_compile_command(
        compiler, export, layout.to_dict(), harness, executable
    )
    completed = run_with_tree_kill(
        command, cwd=harness.parent, env=environment, text=True, timeout=60
    )
    if completed.returncode:
        raise CppAcceptanceError(
            "C++ verifier compilation failed: " + completed.stderr.strip()
        )
    if executable.is_symlink() or not executable.is_file():
        raise CppAcceptanceError("C++ compiler did not produce a regular verifier")
    runtime_environment = dict(environment)
    if os.name == "nt" and runtime_dirs:
        runtime_environment["PATH"] = os.pathsep.join(
            (*runtime_dirs, environment.get("PATH", ""))
        )
    return runtime_environment
