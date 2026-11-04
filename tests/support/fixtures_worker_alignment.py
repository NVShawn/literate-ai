"""A real executable at the external coding-agent boundary for public alignment."""

import os
import unittest
from pathlib import Path

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.cpp import (
    _compiler_process_environment,
    discover_cpp_toolchain,
)
from literate_ai.adapters.builders.python import BuildError

SOURCE = r"""
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
int main(int argc, char** argv) {
    std::string mode;
    auto path = std::filesystem::path(argv[0]).parent_path() / "protocol.txt";
    std::ifstream(path) >> mode;
    if (argc == 3 && std::string(argv[1]) == "run" &&
        std::string(argv[2]) == "--help") {
        if (mode == "helpfail") return 3;
        std::cout << (mode == "v2" ? "  --standalone private server\n" : "  --pure\n");
        return 0;
    }
    int i = 1;
    if (i >= argc || std::string(argv[i++]) != "run") return 4;
    if (mode == "v2") {
        if (i >= argc || std::string(argv[i++]) != "--standalone") return 5;
    }
    if (i + 3 != argc || std::string(argv[i++]) != "-m" ||
        std::string(argv[i++]) != "fixture/model") return 6;
    std::cout << (mode == "wrong" ? "not the requested reply\n" : "ok\n");
    return 0;
}
"""


def build_coding_cli(root: Path) -> Path:
    """Build once per test class; only a genuinely missing compiler may skip."""
    try:
        tool = discover_cpp_toolchain()
    except BuildError as exc:
        if exc.code == "builder.cpp_toolchain_unavailable":
            raise unittest.SkipTest(str(exc)) from exc
        raise
    source = root / "agent.cpp"
    source.write_text(SOURCE, encoding="utf-8")
    binary = root / ("opencode.exe" if os.name == "nt" else "opencode")
    arguments = (
        ("/nologo", "/std:c++17", "/EHsc", "/MT", str(source), "/Fe:" + str(binary))
        if tool.family == "msvc"
        else ("-std=c++17", str(source), "-o", str(binary))
    )
    result = run_bounded_process(
        (*tool.command, *arguments),
        cwd=root,
        environment=_compiler_process_environment(dict(os.environ), tool),
        timeout_seconds=300,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
        error_prefix="fixture.coding-cli",
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return binary
