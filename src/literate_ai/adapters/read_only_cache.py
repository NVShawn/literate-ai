"""Bounded disposable views for tools that mutate disk caches while reading."""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

from literate_ai._filesystem import path_is_link_or_reparse, require_safe_directory


def copy_read_only_cache(
    source: Path,
    destination: Path,
    *,
    maximum_bytes: int,
    retention_seconds: int,
) -> None:
    """Copy cache inputs without linking writable tool files to shared storage.

    An unavailable, changing, or over-budget cache becomes an empty view. The
    caller still verifies restored content and owns disposal of the destination.
    No source entry is touched by cleanup or garbage collection.
    """
    require_safe_directory(destination.parent)
    destination.mkdir()
    if not source.exists():
        return
    require_safe_directory(source)
    remaining = maximum_bytes
    entries = 0
    cutoff = time.time() - retention_seconds
    deadline = time.monotonic() + 30
    try:
        for directory, directories, files in os.walk(source, followlinks=False):
            if time.monotonic() > deadline:
                raise OSError("cache copy deadline exceeded")
            parent = Path(directory)
            require_safe_directory(parent)
            entries += len(directories) + len(files)
            if entries > 100_000:
                raise OSError("cache inventory exceeds bound")
            for name in directories:
                require_safe_directory(parent / name)
            for name in files:
                path = parent / name
                before = path.lstat()
                if path_is_link_or_reparse(path) or not stat.S_ISREG(before.st_mode):
                    raise OSError("unsafe cache entry")
                if before.st_mtime < cutoff:
                    continue
                if before.st_size > remaining:
                    raise OSError("cache exceeds byte bound")
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
                    opened = os.fstat(reader.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (
                        before.st_dev,
                        before.st_ino,
                    ) != (
                        opened.st_dev,
                        opened.st_ino,
                    ):
                        raise OSError("cache entry changed")
                    copied = 0
                    while chunk := reader.read(min(1024 * 1024, remaining + 1)):
                        if time.monotonic() > deadline:
                            raise OSError("cache copy deadline exceeded")
                        copied += len(chunk)
                        remaining -= len(chunk)
                        if remaining < 0 or copied > before.st_size:
                            raise OSError("cache entry exceeds bound")
                        writer.write(chunk)
                    after = os.fstat(reader.fileno())
                    if copied != before.st_size or (
                        before.st_size,
                        before.st_mtime_ns,
                    ) != (after.st_size, after.st_mtime_ns):
                        raise OSError("cache entry changed")
    except OSError:
        shutil.rmtree(destination)
        destination.mkdir()
