"""Verify hydrated indexed LFS content without invoking a content filter."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from literate_ai._cache_lock import _identity_matches
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)

from .repository_orchestration import _git


def _signature(node) -> tuple[int, ...]:
    return (
        node.st_dev,
        node.st_ino,
        node.st_mode,
        node.st_size,
        node.st_mtime_ns,
        node.st_ctime_ns,
        node.st_nlink,
    )


@dataclass(frozen=True, slots=True)
class HydratedLfsObservation:
    path: str
    signature: tuple[int, ...]
    digest: str
    size: int


def observe_hydrated_lfs_entry(
    root: Path, entry: bytes
) -> HydratedLfsObservation | None:
    """Accept only an unstaged content change matching the exact indexed pointer.

    Callers still inspect every other status entry and refuse configured external
    filters before status. A matching hash never excuses a staged or mode change.
    """
    if not entry.startswith(b" M "):
        return None
    name = os.fsdecode(entry[3:])
    attributes = _git(root, "check-attr", "--cached", "-z", "filter", "--", name)
    if attributes.split(b"\0") != [entry[3:], b"filter", b"lfs", b""]:
        return None
    if int(_git(root, "cat-file", "-s", ":./" + name)) > 1024:
        return None
    pointer = _git(root, "show", ":./" + name)
    match = re.fullmatch(
        rb"version https://git-lfs.github.com/spec/v1\n"
        rb"oid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n",
        pointer,
    )
    if match is None:
        return None
    path = root / name
    try:
        require_safe_directory(path.parent)
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat_is_link_or_reparse(before)
            or before.st_size != int(match[2])
        ):
            return None
        indexed = _git(root, "--literal-pathspecs", "ls-files", "--stage", "--", name)
        mode = indexed.split(b" ", 1)[0]
        if os.name != "nt" and bool(before.st_mode & 0o111) != (mode == b"100755"):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not _identity_matches(path, before, descriptor, opened):
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            require_safe_directory(path.parent)
            after = path.lstat()

            if (
                _signature(before) == _signature(after)
                and _signature(opened) == _signature(os.fstat(descriptor))
                and _identity_matches(path, after, descriptor, opened)
                and digest == match[1].decode("ascii")
            ):
                return HydratedLfsObservation(
                    name, _signature(before), digest, before.st_size
                )
            return None
        finally:
            os.close(descriptor)
    except (OSError, UnsafeFilesystemPathError):
        return None


def hydrated_lfs_entry(root: Path, entry: bytes) -> bool:
    return isinstance(observe_hydrated_lfs_entry(root, entry), HydratedLfsObservation)


def require_hydrated_lfs_unchanged(
    root: Path, observation: HydratedLfsObservation
) -> None:
    """Revalidate exact admitted payload bytes and physical node custody."""
    path = root / observation.path
    require_safe_directory(path.parent)
    before = path.lstat()
    if (
        _signature(before) != observation.signature
        or not stat.S_ISREG(before.st_mode)
        or stat_is_link_or_reparse(before)
        or before.st_size != observation.size
    ):
        raise OSError("hydrated LFS node custody changed")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not _identity_matches(path, before, descriptor, opened):
            raise OSError("hydrated LFS node identity changed")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.lstat()
        opened_after = os.fstat(descriptor)
        if (
            digest != observation.digest
            or _signature(after) != observation.signature
            or _signature(opened_after) != _signature(opened)
            or not _identity_matches(path, after, descriptor, opened_after)
        ):
            raise OSError("hydrated LFS payload changed")
    finally:
        os.close(descriptor)
