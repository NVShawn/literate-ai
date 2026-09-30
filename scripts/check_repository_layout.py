"""Fail when generated dependency/build state enters Git or Python root code returns."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

_FORBIDDEN_PARTS = {"_build", "__pycache__", "node_modules"}


class LayoutError(RuntimeError):
    """Repository layout violates an authored/generated boundary."""


def _git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise LayoutError(completed.stderr.decode("utf-8", errors="replace").strip())
    return completed.stdout


def _forbidden(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(_FORBIDDEN_PARTS.intersection(parts)) or path.endswith((".pyc", ".pyo"))


def validate_repository_layout(root: Path) -> None:
    root = root.resolve(strict=True)
    visible = {
        raw.decode("utf-8")
        for raw in _git(
            root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
        ).split(b"\0")
        if raw
    }
    deleted = {
        raw.decode("utf-8")
        for raw in _git(root, "ls-files", "-z", "--deleted").split(b"\0")
        if raw
    }
    current = sorted(visible - deleted)
    forbidden_current = [path for path in current if _forbidden(path)]
    root_python = [path for path in current if "/" not in path and path.endswith(".py")]

    forbidden_history: list[str] = []
    for line in _git(root, "rev-list", "--objects", "--all").splitlines():
        fields = line.split(b" ", 1)
        if len(fields) == 2:
            path = fields[1].decode("utf-8")
            if _forbidden(path):
                forbidden_history.append(path)

    problems: list[str] = []
    if (root / "__pycache__").exists():
        problems.append("root __pycache__ exists; direct bytecode into OBJ_DIR")
    if forbidden_current:
        problems.append(
            "generated paths are Git-visible: " + ", ".join(forbidden_current)
        )
    if root_python:
        problems.append(
            "root importable Python modules are forbidden: " + ", ".join(root_python)
        )
    if forbidden_history:
        problems.append(
            "generated paths exist in reachable Git history: "
            + ", ".join(sorted(set(forbidden_history)))
        )
    if problems:
        raise LayoutError("; ".join(problems))


def main() -> int:
    validate_repository_layout(Path.cwd())
    print("Repository layout: authored/generated boundaries are clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
