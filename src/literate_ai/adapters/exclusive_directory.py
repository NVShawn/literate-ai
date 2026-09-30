"""Publish a staged directory without replacing a concurrent destination.

This is an internal cooperative-custody primitive, not hostile-process containment.
It does not create parents, delete staging, grant trust, or execute package members.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.contracts.paths import canonical_relative_posix_path


def directory_node(path: Path) -> tuple[int, int, int]:
    require_safe_directory(path)
    node = path.lstat()
    if stat_is_link_or_reparse(node) or not stat.S_ISDIR(node.st_mode):
        raise ValueError("retained.publication.directory-unsafe")
    return node.st_dev, node.st_ino, node.st_mode


def _native_rename(parent: int, source: bytes, destination: bytes) -> None:
    if sys.platform == "darwin":
        symbol, flag = "renameatx_np", 4  # RENAME_EXCL, Darwin sys/stdio.h
    elif sys.platform.startswith("linux"):
        symbol, flag = "renameat2", 1  # RENAME_NOREPLACE, Linux stdio.h
    else:
        raise OSError(errno.ENOTSUP, "exclusive directory rename unavailable")
    function = getattr(ctypes.CDLL(None, use_errno=True), symbol, None)
    if function is None:
        raise OSError(errno.ENOTSUP, "exclusive directory rename unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(parent, source, parent, destination, flag) != 0:
        number = ctypes.get_errno()
        raise OSError(number, "exclusive directory rename refused")


def publish_directory_exclusive(
    source: Path,
    destination: Path,
    *,
    expected_source: tuple[int, int, int],
    expected_parent: tuple[int, int, int],
) -> None:
    """Move one owned stage within its pinned parent; never replace a peer.

    Callers must hold their write reservations and verify complete stage bytes
    before and after publication. On an error they must inspect custody before
    cleanup: this function deliberately never removes either pathname.
    """
    source, destination = Path(source), Path(destination)
    if (
        not source.is_absolute()
        or not destination.is_absolute()
        or source.parent != destination.parent
        or source.name.casefold() == destination.name.casefold()
    ):
        raise ValueError("retained.publication.path-invalid")
    for name in (source.name, destination.name):
        canonical_relative_posix_path(name, label="publication name")
    parent_path = source.parent

    def guard():
        if directory_node(parent_path) != expected_parent:
            raise ValueError("retained.publication.parent-changed")

    guard()
    if directory_node(source) != expected_source:
        raise ValueError("retained.publication.stage-changed")
    for index, entry in enumerate(parent_path.iterdir()):
        if index >= 16384:
            raise ValueError("retained.publication.directory-limit")
        if entry.name.casefold() == destination.name.casefold():
            raise FileExistsError(errno.EEXIST, "publication destination exists")
    guard()
    if os.name == "nt":
        # Python guarantees FileExistsError for an existing Windows destination.
        os.rename(source, destination)
    else:
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(parent_path.anchor, flags)
            stack.callback(os.close, descriptor)
            for name in parent_path.parts[1:]:
                descriptor = os.open(name, flags, dir_fd=descriptor)
                stack.callback(os.close, descriptor)
            node = os.fstat(descriptor)
            if (node.st_dev, node.st_ino, node.st_mode) != expected_parent:
                raise ValueError("retained.publication.parent-changed")
            node = os.stat(source.name, dir_fd=descriptor, follow_symlinks=False)
            if (node.st_dev, node.st_ino, node.st_mode) != expected_source:
                raise ValueError("retained.publication.stage-changed")
            _native_rename(
                descriptor, os.fsencode(source.name), os.fsencode(destination.name)
            )
    guard()
    if directory_node(destination) != expected_source:
        raise ValueError("retained.publication.published-node-changed")
