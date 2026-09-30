"""Regression coverage for `exclusive_cache_lock`'s TOCTOU swap detection.

Issue #76: the swap-detection guard compared `(st_dev, st_ino)` of the lock
path against the open descriptor. Windows `st_ino` is not a stable unique
file identity across every volume and Python version, so that comparison
could either become a no-op (a swapped/reparse-point lock file compares
equal and is treated as safe) or a flake (a spurious abort on an unchanged
file). The fix routes the Windows-side comparison through
`GetFileInformationByHandle` file-index/volume identity instead of `st_ino`.
"""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai import _cache_lock
from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock


class _FakeKernel32Windows:
    """Minimal stand-in for `_cache_lock._Kernel32Windows`.

    Mimics just enough of `open_reparse_aware`/`file_identity`/`close` to
    exercise the Windows identity-comparison code path from a non-Windows CI
    host, without touching any real Win32 API. `identities` maps a native
    handle (as produced by `open_reparse_aware` or supplied directly for the
    descriptor side) to the `(volume_serial_number, file_index_high,
    file_index_low)` tuple `GetFileInformationByHandle` would report for it.
    """

    def __init__(self, path_identity_by_path: dict[str, tuple[int, int, int]]) -> None:
        self.path_identity_by_path = path_identity_by_path
        self.opened: list[str] = []
        self.closed: list[int] = []
        self._next_handle = 1000
        self._handle_paths: dict[int, str] = {}

    def open_reparse_aware(self, path: str) -> int:
        self.opened.append(path)
        if path not in self.path_identity_by_path:
            raise OSError(2, f"no such file: {path}")
        self._next_handle += 1
        handle = self._next_handle
        self._handle_paths[handle] = path
        return handle

    def close(self, handle: int) -> None:
        self.closed.append(handle)

    def file_identity(self, handle: int) -> tuple[int, int, int]:
        if handle in self._handle_paths:
            return self.path_identity_by_path[self._handle_paths[handle]]
        # The descriptor side is queried by a synthetic handle equal to the
        # identity tuple's index into a fixed table set up by the test.
        return self.path_identity_by_path["__descriptor__"]


class WindowsIdentityMatchesTests(unittest.TestCase):
    def test_fails_closed_when_the_path_is_gone(self) -> None:
        kernel = _FakeKernel32Windows({"__descriptor__": (7, 0, 42)})

        matches = _cache_lock._windows_identity_matches(
            Path("C:\\lock"),
            99,
            kernel32=kernel,
            get_osfhandle=lambda descriptor: descriptor,
        )

        self.assertFalse(matches)


class ExclusiveCacheLockWindowsTests(unittest.TestCase):
    """End-to-end coverage of `exclusive_cache_lock` under a faked Windows OS."""

    def test_publishes_normally_when_windows_identity_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "lock"
            kernel = _FakeKernel32Windows({})

            def fake_kernel32() -> _FakeKernel32Windows:
                # Every open of the (unchanged) lock path reports the same
                # identity as the descriptor: the common, non-swapped case.
                kernel.path_identity_by_path[str(lock_path)] = (3, 0, 7)
                kernel.path_identity_by_path["__descriptor__"] = (3, 0, 7)
                return kernel

            with (
                patch.object(_cache_lock, "_is_windows", lambda: True),
                patch.object(_cache_lock, "_default_windows_kernel32", fake_kernel32),
                patch.object(
                    _cache_lock, "_default_get_osfhandle", lambda descriptor: descriptor
                ),
            ):
                entered = False
                with exclusive_cache_lock(lock_path):
                    entered = True
                self.assertTrue(entered)

    def test_accepts_peer_that_publishes_and_locks_placeholder_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "lock"
            kernel = _FakeKernel32Windows({})
            real_write = os.write

            def fake_kernel32() -> _FakeKernel32Windows:
                kernel.path_identity_by_path[str(lock_path)] = (3, 0, 7)
                kernel.path_identity_by_path["__descriptor__"] = (3, 0, 7)
                return kernel

            def peer_writes_first(descriptor: int, content: bytes) -> int:
                real_write(descriptor, content)
                raise PermissionError(errno.EACCES, "peer acquired byte-range lock")

            with (
                patch.object(_cache_lock, "_is_windows", lambda: True),
                patch.object(_cache_lock, "_default_windows_kernel32", fake_kernel32),
                patch.object(
                    _cache_lock, "_default_get_osfhandle", lambda descriptor: descriptor
                ),
                patch.object(_cache_lock.os, "write", side_effect=peer_writes_first),
            ):
                with exclusive_cache_lock(lock_path):
                    pass

            self.assertEqual(lock_path.read_bytes(), b"\0")

    def test_aborts_publication_when_windows_identity_diverges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "lock"
            kernel = _FakeKernel32Windows({})

            def fake_kernel32() -> _FakeKernel32Windows:
                # The descriptor names a different file-index than the path
                # now resolves to: bare st_ino could miss this on Windows.
                kernel.path_identity_by_path[str(lock_path)] = (3, 0, 999)
                kernel.path_identity_by_path["__descriptor__"] = (3, 0, 7)
                return kernel

            with (
                patch.object(_cache_lock, "_is_windows", lambda: True),
                patch.object(_cache_lock, "_default_windows_kernel32", fake_kernel32),
                patch.object(
                    _cache_lock, "_default_get_osfhandle", lambda descriptor: descriptor
                ),
            ):
                with self.assertRaises(CacheLockError):
                    with exclusive_cache_lock(lock_path):
                        self.fail("must not publish when identity diverges")


class ExclusiveCacheLockPosixTests(unittest.TestCase):
    """Confirms the POSIX guard still catches a hard-link swap directly."""

    @unittest.skipIf(os.name == "nt", "hard links exercised via POSIX semantics")
    def test_hard_link_replacement_is_detected_as_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory).resolve() / "lock"
            other_path = Path(directory) / "other"
            lock_path.write_bytes(b"")
            other_path.write_bytes(b"")

            original_open = os.open

            def swap_after_open(path, flags, mode=0o777, *args, **kwargs):
                descriptor = original_open(path, flags, mode, *args, **kwargs)
                if Path(path) == lock_path:
                    lock_path.unlink()
                    os.link(other_path, lock_path)
                return descriptor

            with patch.object(os, "open", side_effect=swap_after_open):
                with self.assertRaises(CacheLockError):
                    with exclusive_cache_lock(lock_path):
                        self.fail("must not publish once the lock path is swapped")


if __name__ == "__main__":
    unittest.main()
