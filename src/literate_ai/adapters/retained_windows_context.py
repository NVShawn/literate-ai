"""Live Windows system context for retained commands, without caller injection."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.exclusive_directory import directory_node


def _system_windows_directory() -> Path:
    if os.name != "nt":
        raise ValueError("retained.windows.native-host-required")
    from ctypes import wintypes

    # Use the actual shared OS directory, not a caller-controlled environment
    # variable or Terminal Services' per-user Windows directory. Restrict DLL
    # lookup to the system directory even when the caller poisons its PATH.
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
    query = kernel.GetSystemWindowsDirectoryW
    query.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    query.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = query(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise ValueError("retained.windows.system-directory-unavailable")
    value = buffer.value
    # Windows documents a drive-only result when Windows occupies its root.
    if len(value) == 2 and value[1] == ":":
        value += "\\"
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("retained.windows.system-directory-unsafe")
    return path


@dataclass(frozen=True, slots=True)
class RetainedWindowsSystemContext:
    root: Path
    root_node: tuple[int, int, int]
    system_node: tuple[int, int, int]

    @classmethod
    def capture(cls):
        root = _system_windows_directory()
        value = cls(root, directory_node(root), directory_node(root / "System32"))
        value.require_current()
        return value

    def require_current(self):
        if (
            _system_windows_directory() != self.root
            or directory_node(self.root) != self.root_node
            or directory_node(self.root / "System32") != self.system_node
        ):
            raise ValueError("retained.windows.system-context-changed")

    def environment(self):
        self.require_current()
        return {"SystemRoot": str(self.root)}


def retained_system_context():
    return RetainedWindowsSystemContext.capture() if os.name == "nt" else None
