#!/usr/bin/env python3
"""Reject new ambient host-path policy outside its explicit owner surfaces."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_HOME_EXCEPTIONS = {
    # This is a target-side '~/' transport expression, not a coordinator host default.
    "scripts/fanout_samples.py",
}
_ENVIRONMENT_EXCEPTIONS = {
    # Bazelisk is an external tool whose own cache variables are its protocol.
    "src/literate_ai/adapters/builders/bazel.py",
    # Coding subprocess environment capture is protocol data, not path selection.
    "src/literate_ai/adapters/models/coding_cli.py",
    # Migration reads old locations by definition.
    "src/literate_ai/adapters/user_config_migration.py",
}
_HOST_ENVIRONMENT_NAMES = {
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}
_HOST_PATH_CONSTRUCTORS = {
    "Path",
    "PurePath",
    "PurePosixPath",
    "PureWindowsPath",
    "pathlib.Path",
    "pathlib.PurePath",
    "pathlib.PurePosixPath",
    "pathlib.PureWindowsPath",
}
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def violations(repository: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for base in (repository / "src", repository / "scripts"):
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(repository).as_posix()
            if relative == "src/literate_ai/adapters/user_paths.py":
                continue
            try:
                tree = ast.parse(path.read_bytes(), filename=relative)
            except (OSError, SyntaxError) as exc:
                findings.append(f"{relative}: cannot inspect host paths: {exc}")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node.func)
                if name == "Path.home" and relative not in _HOME_EXCEPTIONS:
                    findings.append(
                        f"{relative}:{node.lineno}: Path.home() bypasses HostPaths"
                    )
                if (
                    name in _HOST_PATH_CONSTRUCTORS
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and (
                        node.args[0].value.startswith(("/", "~/", "\\\\"))
                        or _WINDOWS_ABSOLUTE_PATH.match(node.args[0].value)
                    )
                ):
                    findings.append(
                        f"{relative}:{node.lineno}: absolute Path literal bypasses "
                        "HostPaths"
                    )
                if (
                    name in {"os.environ.get", "environment.get"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in _HOST_ENVIRONMENT_NAMES
                    and relative not in _ENVIRONMENT_EXCEPTIONS
                ):
                    findings.append(
                        f"{relative}:{node.lineno}: {node.args[0].value} "
                        "bypasses HostPaths"
                    )
    return tuple(findings)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    repository = Path(arguments[0] if arguments else ".").resolve(strict=True)
    findings = violations(repository)
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print(
        "host path policy: no ambient defaults outside declared "
        "owner/exception surfaces"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
