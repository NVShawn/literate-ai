"""Canonical portable paths shared by domain and filesystem boundaries."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from pathlib import PurePosixPath, PureWindowsPath

_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "aux",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        *(f"com{index}" for index in "¹²³"),
        *(f"lpt{index}" for index in "¹²³"),
    }
)
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"\\|?*')


def canonical_relative_posix_path(value: str, *, label: str) -> PurePosixPath:
    """Return the single cross-platform representation of one relative path.

    POSIX parsing alone treats Windows drive-relative paths, device names, alternate
    data streams, and backslashes as ordinary names. Applying the strictest shared
    representation on every host keeps one admitted identity from addressing another
    object when the tree is materialized on a different supported platform.
    """

    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must use NFC Unicode normalization")
    if "\\" in value:
        raise ValueError(f"{label} must use canonical POSIX separators")
    if _WINDOWS_DRIVE_PREFIX.match(value) or PureWindowsPath(value).drive:
        raise ValueError(f"{label} cannot use a Windows drive or device path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != value
    ):
        raise ValueError(f"{label} must be normalized and relative")
    for part in relative.parts:
        if any(
            character in _WINDOWS_FORBIDDEN_CHARACTERS or ord(character) < 32
            for character in part
        ) or part.endswith((" ", ".")):
            raise ValueError(f"{label} is not portable to Windows")
        device_name = part.split(".", 1)[0].rstrip(" ").casefold()
        if device_name in _WINDOWS_DEVICE_NAMES:
            raise ValueError(f"{label} cannot use a Windows device name")
    return relative


def canonical_relative_posix_paths(
    values: Iterable[str], *, label: str
) -> tuple[PurePosixPath, ...]:
    """Validate a logical file-tree path set, including cross-host aliases.

    A collection is not a materializable file tree when two names alias on a
    case-insensitive filesystem, or when one admitted file is another file's parent.
    The latter check also uses the portable alias key so the result is independent of
    the filesystem on which validation happens.
    """

    paths = tuple(canonical_relative_posix_path(value, label=label) for value in values)
    aliases: dict[tuple[str, ...], PurePosixPath] = {}
    for path in sorted(paths, key=lambda item: item.as_posix()):
        alias = tuple(
            unicodedata.normalize("NFC", part.casefold()) for part in path.parts
        )
        previous = aliases.get(alias)
        if previous is not None:
            raise ValueError(
                f"{label} values {previous.as_posix()!r} and "
                f"{path.as_posix()!r} alias on a supported filesystem"
            )
        aliases[alias] = path
    for alias, path in aliases.items():
        for depth in range(1, len(alias)):
            ancestor = aliases.get(alias[:depth])
            if ancestor is not None:
                raise ValueError(
                    f"{label} value {ancestor.as_posix()!r} is a file ancestor of "
                    f"{path.as_posix()!r}"
                )
    return paths


__all__ = ["canonical_relative_posix_path", "canonical_relative_posix_paths"]
