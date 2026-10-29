"""Verified, disposable Cargo intermediates and timestamp-preserving projections."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.contracts import canonical_identity

from .standard_local import LocalStandardLifecycleError


def workspace_digest(root: Path) -> str:
    """Rehash files and freshness metadata before trusting Cargo's fingerprints."""
    require_safe_directory(root)
    records = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat_is_link_or_reparse(metadata) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise LocalStandardLifecycleError("Cargo workspace contains an unsafe node")
        record = {
            "path": path.relative_to(root).as_posix(),
            "mode": stat.S_IMODE(metadata.st_mode),
            "mtime_ns": metadata.st_mtime_ns,
        }
        if stat.S_ISREG(metadata.st_mode):
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            record.update(
                sha256=digest,
            )
        records.append(record)
    return canonical_identity({"cargo_workspace": records}).uri


def sync_projection(source: Path, destination: Path) -> None:
    """Remove stale inputs while preserving timestamps of identical regular files.

    The caller verifies the previous workspace before entering this function.
    Changed files receive current timestamps, even when restored source has old ones.
    Cargo.lock is deliberately removed: each authorized build derives its own lock.
    """
    destination.mkdir(parents=True, exist_ok=True)
    expected = {path.name: path for path in source.iterdir()}
    for path in destination.iterdir():
        original = expected.get(path.name)
        if original is None or path.is_dir() != original.is_dir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    for name, original in expected.items():
        path = destination / name
        if original.is_dir():
            sync_projection(original, path)
        else:
            if not path.exists() or path.read_bytes() != original.read_bytes():
                shutil.copyfile(original, path)
                os.utime(path, None)
            path.chmod(stat.S_IMODE(original.stat().st_mode))
