"""Project one authored Node tool into the disposable object directory."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import tempfile
from pathlib import Path


class StagingError(RuntimeError):
    """The authored tool tree or disposable destination is unsafe."""


def _regular_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name for name in directories if name not in {"node_modules", "__pycache__"}
        )
        for name in (*directories, *names):
            candidate = current_path / name
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise StagingError(f"tool source contains a symbolic link: {candidate}")
            if candidate.is_dir():
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise StagingError(f"tool source contains a special file: {candidate}")
            files.append(candidate)
    return tuple(sorted(files))


def stage_node_tool(source: Path, destination: Path, object_root: Path) -> None:
    source = source.resolve(strict=True)
    object_root = object_root.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise StagingError("tool source must be a directory")
    if destination == object_root or not destination.is_relative_to(object_root):
        raise StagingError("tool destination must be a child of OBJ_DIR")
    object_root.mkdir(parents=True, exist_ok=True)
    files = _regular_files(source)
    temporary = Path(tempfile.mkdtemp(prefix="node-tool-", dir=object_root))
    projected = temporary / "project"
    projected.mkdir()
    try:
        for source_file in files:
            relative = source_file.relative_to(source)
            target = projected / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target)
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise StagingError("tool destination is not a replaceable directory")
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(projected, destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--object-root", required=True, type=Path)
    arguments = parser.parse_args()
    stage_node_tool(arguments.source, arguments.destination, arguments.object_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
