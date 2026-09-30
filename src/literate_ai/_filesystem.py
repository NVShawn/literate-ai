"""Small, portable filesystem-node safety predicates."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path

_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_NATIVE_OR_WASM_SUFFIXES = (
    ".a",
    ".dll",
    ".dylib",
    ".exe",
    ".lib",
    ".node",
    ".o",
    ".obj",
    ".so",
    ".wasm",
)
_NATIVE_OR_WASM_MAGICS = (
    b"\x00asm",
    b"\x7fELF",
    b"MZ",
    b"!<arch>\n",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xbe\xba\xfe\xca",
    b"\xbf\xba\xfe\xca",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
)


class UnsafeFilesystemPathError(RuntimeError):
    """A directory path would traverse a link, reparse point, or non-directory."""


def stat_is_link_or_reparse(metadata: os.stat_result) -> bool:
    """Return whether lstat metadata names a symlink or Windows reparse point."""

    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _WINDOWS_REPARSE_POINT)


def path_is_link_or_reparse(path: Path) -> bool:
    """Inspect one path without following its final component."""

    try:
        metadata = Path(path).lstat()
    except FileNotFoundError:
        return False
    return stat_is_link_or_reparse(metadata)


def name_or_magic_is_native_or_wasm(name: str, magic: bytes) -> bool:
    """Recognize portable native-object and WebAssembly payload indicators."""

    folded_name = Path(name).name.casefold()
    return (
        folded_name.endswith(_NATIVE_OR_WASM_SUFFIXES)
        or ".so." in folded_name
        or magic.startswith(_NATIVE_OR_WASM_MAGICS)
    )


def ensure_safe_directory(path: Path, *, mode: int = 0o700) -> Path:
    """Create an absolute directory path without following existing link-like nodes.

    POSIX traversal is descriptor-relative and uses ``O_NOFOLLOW`` for every existing
    component.  Windows has no equivalent operation in Python's portable ``os`` API,
    so it validates every component before and after one-at-a-time creation and rejects
    junctions and other reparse points.
    """

    directory = Path(path)
    if not directory.is_absolute():
        raise UnsafeFilesystemPathError("safe directory path must be absolute")
    if os.name == "nt":
        _ensure_safe_directory_windows(directory, mode=mode)
    else:
        _ensure_safe_directory_posix(directory, mode=mode)
    return directory


def require_safe_directory(path: Path, *, allow_missing: bool = False) -> Path:
    """Validate directory ancestors without writes; optionally allow missing suffixes.

    Missing suffixes are useful for read-only stores that must report absent data.
    Existing ancestors still cannot be links, reparse points or regular files.
    """

    directory = Path(path)
    if not directory.is_absolute():
        raise UnsafeFilesystemPathError("safe directory path must be absolute")
    if any(part == ".." for part in directory.parts):
        raise UnsafeFilesystemPathError("safe directory path must not traverse parents")
    if os.name == "nt":
        _ensure_safe_directory_windows(
            directory, mode=0o700, create=False, allow_missing=allow_missing
        )
    else:
        _ensure_safe_directory_posix(
            directory, mode=0o700, create=False, allow_missing=allow_missing
        )
    return directory


def _ensure_safe_directory_posix(
    directory: Path, *, mode: int, create: bool = True, allow_missing: bool = False
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory.anchor, flags | nofollow)
    try:
        for part in directory.parts[1:]:
            if part in {"", ".", ".."} or "/" in part:
                raise UnsafeFilesystemPathError(
                    f"unsafe directory path component: {part!r}"
                )
            try:
                child = os.open(part, flags | nofollow, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    if allow_missing:
                        return
                    raise UnsafeFilesystemPathError(
                        f"safe directory is unavailable: {directory}"
                    ) from None
                try:
                    os.mkdir(part, mode=mode, dir_fd=descriptor)
                except FileExistsError:
                    # A concurrent creator won. Opening it below still proves that it
                    # is a directory and not a link.
                    pass
                try:
                    child = os.open(part, flags | nofollow, dir_fd=descriptor)
                except OSError as exc:
                    raise UnsafeFilesystemPathError(
                        f"directory component became unsafe: {part}"
                    ) from exc
            except OSError as exc:
                raise UnsafeFilesystemPathError(
                    f"directory component is unsafe: {part}"
                ) from exc
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise UnsafeFilesystemPathError(
                    f"directory component is not a directory: {part}"
                )
            os.close(descriptor)
            descriptor = child
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _ensure_safe_directory_windows(
    directory: Path, *, mode: int, create: bool = True, allow_missing: bool = False
) -> None:
    current = Path(directory.anchor)
    for part in directory.parts[1:]:
        if part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise UnsafeFilesystemPathError(
                f"unsafe directory path component: {part!r}"
            )
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                if allow_missing:
                    return
                raise UnsafeFilesystemPathError(
                    f"safe directory is unavailable: {directory}"
                ) from None
            try:
                current.mkdir(mode=mode, parents=False, exist_ok=False)
            except FileExistsError:
                pass
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise UnsafeFilesystemPathError(
                    f"directory component is unavailable: {current}"
                ) from exc
        except OSError as exc:
            raise UnsafeFilesystemPathError(
                f"directory component is unavailable: {current}"
            ) from exc
        if stat_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeFilesystemPathError(f"directory component is unsafe: {current}")


__all__ = [
    "UnsafeFilesystemPathError",
    "ensure_safe_directory",
    "name_or_magic_is_native_or_wasm",
    "path_is_link_or_reparse",
    "require_safe_directory",
    "stat_is_link_or_reparse",
]
