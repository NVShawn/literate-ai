"""Bounded PE machine observations, without loading or executing an image."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path


def pe_architecture(
    read: Callable[[int, int], bytes], *, context: str = "Windows image"
) -> str:
    """Read only DOS/COFF headers; callers own file custody and execution policy."""
    dos = read(0, 64)
    if len(dos) != 64 or dos[:2] != b"MZ":
        raise ValueError(f"{context} has no complete PE DOS header")
    offset = int.from_bytes(dos[60:64], "little")
    if offset < 64:
        raise ValueError(f"{context} has an invalid PE header offset")
    header = read(offset, 24)
    if len(header) != 24 or header[:4] != b"PE\0\0":
        raise ValueError(f"{context} has no complete PE COFF header")
    machine = int.from_bytes(header[4:6], "little")
    # ARM64EC/ARM64X are deliberately distinct from a pure ARM64 image.
    # https://learn.microsoft.com/windows/win32/debug/pe-format#machine-types
    try:
        return {
            0x014C: "x86",
            0x8664: "x86_64",
            0xAA64: "arm64",
            0xA641: "arm64ec",
            0xA64E: "arm64x",
        }[machine]
    except KeyError as exc:
        raise ValueError(f"{context} has an unsupported PE machine type") from exc


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        getattr(value, "st_file_attributes", 0),
    )


def observe_pe_architecture(path: Path) -> str:
    """Inspect one stable regular image through one descriptor, reading 88 bytes.

    This proves the machine field of this file, not loader closure, ABI, or the
    architecture of arbitrary subprocesses a launcher might later execute.
    """
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or (
        getattr(before, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise ValueError("Windows image is not a regular non-reparse file")
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(path, flags)
    try:
        if _identity(before) != _identity(os.fstat(descriptor)):
            raise ValueError("Windows image changed while opening")

        def read(offset: int, limit: int) -> bytes:
            os.lseek(descriptor, offset, os.SEEK_SET)
            chunks = []
            while limit:
                chunk = os.read(descriptor, limit)
                if not chunk:
                    break
                chunks.append(chunk)
                limit -= len(chunk)
            return b"".join(chunks)

        architecture = pe_architecture(read)
        if _identity(before) != _identity(os.fstat(descriptor)) or _identity(
            before
        ) != _identity(path.stat(follow_symlinks=False)):
            raise ValueError("Windows image changed while reading")
        return architecture
    finally:
        os.close(descriptor)
