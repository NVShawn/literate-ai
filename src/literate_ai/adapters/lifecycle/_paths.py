"""Host-independent canonical paths for lifecycle-controlled trees."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from literate_ai.contracts.paths import (
    canonical_relative_posix_path,
    canonical_relative_posix_paths,
)


def contained_materialization_target(
    root: Path, relative: PurePosixPath, *, label: str
) -> Path:
    """Resolve a target parent and prove it remains beneath a regular root."""

    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{label} root must be a regular directory")
    resolved_root = root.resolve(strict=True)
    target = resolved_root.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = target.parent.resolve(strict=True)
    if not resolved_parent.is_relative_to(resolved_root):
        raise ValueError(f"{label} escapes its materialization root")
    return resolved_parent / target.name


def require_materialized_file_contained(
    root: Path, target: Path, *, label: str
) -> None:
    """Verify a newly materialized regular file did not escape through an alias."""

    resolved_root = root.resolve(strict=True)
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"{label} must materialize as a regular file")
    resolved_target = target.resolve(strict=True)
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(f"{label} escapes its materialization root")


__all__ = [
    "canonical_relative_posix_path",
    "canonical_relative_posix_paths",
    "contained_materialization_target",
    "require_materialized_file_contained",
]
