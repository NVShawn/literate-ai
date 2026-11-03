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
import unicodedata
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


def _native_rename(
    parent: int,
    source: bytes,
    destination: bytes,
    destination_parent: int | None = None,
) -> None:
    if sys.platform == "darwin":
        symbol, flag = "renameatx_np", 4  # RENAME_EXCL, Darwin sys/stdio.h
    elif sys.platform.startswith("linux"):
        symbol, flag = "renameat2", 1  # RENAME_NOREPLACE, Linux stdio.h
    elif sys.platform.startswith("freebsd"):
        symbol, flag = "renameat2", 1  # AT_RENAME_NOREPLACE, FreeBSD sys/fcntl.h
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
    target_parent = parent if destination_parent is None else destination_parent
    if function(parent, source, target_parent, destination, flag) != 0:
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


def publish_directory_between_owned_parents_exclusive(
    source: Path,
    destination: Path,
    *,
    expected_source: tuple[int, int, int],
    expected_source_parent: tuple[int, int, int],
    expected_destination_parent: tuple[int, int, int],
) -> None:
    """Move an owned stage between pinned parents without replacing a peer.

    This distinct operation does not widen the same-parent v1 publication
    contract. The caller owns both parents, complete package-byte verification,
    reservations and any rollback. Cross-device moves refuse; no copy, merge or
    deletion fallback can expose partial files or discard a concurrent change.
    """
    source, destination = Path(source), Path(destination)
    if (
        not source.is_absolute()
        or not destination.is_absolute()
        or ".." in source.parts
        or ".." in destination.parts
        or source == destination
        or source.is_relative_to(destination)
        or destination.is_relative_to(source)
    ):
        raise ValueError("retained.publication.path-invalid")
    for path in (source, destination):
        canonical_relative_posix_path(path.name, label="publication name")

    def guard():
        if (
            directory_node(source.parent) != expected_source_parent
            or directory_node(destination.parent) != expected_destination_parent
        ):
            raise ValueError("retained.publication.parent-changed")

    guard()
    if directory_node(source) != expected_source:
        raise ValueError("retained.publication.stage-changed")
    target_key = unicodedata.normalize("NFC", destination.name.casefold())
    for index, entry in enumerate(destination.parent.iterdir()):
        if index >= 16384:
            raise ValueError("retained.publication.directory-limit")
        if unicodedata.normalize("NFC", entry.name.casefold()) == target_key:
            raise FileExistsError(errno.EEXIST, "publication destination exists")
    guard()
    if os.name == "nt":
        # Windows rename refuses an existing destination and cross-volume moves.
        os.rename(source, destination)
    else:
        with ExitStack() as stack:

            def open_parent(path, expected):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptor = os.open(path.anchor, flags)
                stack.callback(os.close, descriptor)
                for name in path.parts[1:]:
                    descriptor = os.open(name, flags, dir_fd=descriptor)
                    stack.callback(os.close, descriptor)
                node = os.fstat(descriptor)
                if (node.st_dev, node.st_ino, node.st_mode) != expected:
                    raise ValueError("retained.publication.parent-changed")
                return descriptor

            source_descriptor = open_parent(source.parent, expected_source_parent)
            target_descriptor = open_parent(
                destination.parent, expected_destination_parent
            )
            node = os.stat(source.name, dir_fd=source_descriptor, follow_symlinks=False)
            if (node.st_dev, node.st_ino, node.st_mode) != expected_source:
                raise ValueError("retained.publication.stage-changed")
            guard()
            _native_rename(
                source_descriptor,
                os.fsencode(source.name),
                os.fsencode(destination.name),
                target_descriptor,
            )
    guard()
    if directory_node(destination) != expected_source:
        raise ValueError("retained.publication.published-node-changed")
