"""Cross-process locks for immutable local-cache publication."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    require_safe_directory,
    stat_is_link_or_reparse,
)

_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.01


class CacheLockError(RuntimeError):
    """A cache publication lock could not be acquired safely."""


def cache_lock_path(
    project_root: Path,
    cache_root: Path,
    *parts: str,
) -> Path:
    """Return a stable lock path outside every removable cache root."""

    project = Path(project_root).resolve(strict=True)
    cache = Path(cache_root).resolve(strict=False)
    if not parts or any(
        not part or part in {".", ".."} or "/" in part or "\\" in part for part in parts
    ):
        raise CacheLockError("cache lock path components are invalid")
    identity = hashlib.sha256(str(cache).encode("utf-8")).hexdigest()
    return project / ".litai-cache-locks" / identity / Path(*parts)


def _open_lock_with_retry(lock_path: Path, flags: int) -> int:
    """Open the lock file, retrying a transient Windows delete-pending race.

    On Windows, when one thread's ``_remove_idle_lock`` has unlinked the lock
    file but a handle is still closing, the inode enters a delete-pending state
    and a concurrent ``os.open(O_CREAT)`` on the same path fails with
    ``PermissionError`` (EACCES) until the pending delete completes. That is
    transient contention, not a real permission error, so retry within a bounded
    deadline. POSIX has no delete-pending state; an EACCES there is a genuine
    permission failure and is raised immediately.
    """

    if not _is_windows():
        return os.open(lock_path, flags, 0o600)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            return os.open(lock_path, flags, 0o600)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_LOCK_RETRY_SECONDS)


@contextmanager
def exclusive_cache_lock(
    path: Path,
    *,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    on_locked: Callable[[int], None] | None = None,
) -> Iterator[None]:
    """Hold one same-host advisory lock until the publication decision is complete.

    ``timeout_seconds`` bounds how long a contended acquisition retries before
    raising :class:`CacheLockError`; the default preserves the original 30s
    publication-lock behavior. ``on_locked``, if given, runs once the OS-level
    lock is held (passed the open descriptor) and before the safety re-check
    and ``yield`` -- callers use it to publish owner diagnostics or to signal a
    deterministic test synchronization point while still holding the lock.
    """

    lock_path = Path(path).absolute()
    try:
        ensure_safe_directory(lock_path.parent)
    except UnsafeFilesystemPathError as exc:
        raise CacheLockError(f"cache lock ancestor is unsafe: {lock_path}") from exc
    try:
        existing = lock_path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise CacheLockError(f"cache lock is unavailable: {lock_path}") from exc
    if existing is not None and (
        stat_is_link_or_reparse(existing) or not stat.S_ISREG(existing.st_mode)
    ):
        raise CacheLockError(f"cache lock is unsafe: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = _open_lock_with_retry(lock_path, flags)
    except OSError as exc:
        raise CacheLockError(f"cache lock is unsafe: {lock_path}") from exc
    locked = False
    try:
        path_metadata = lock_path.lstat()
        descriptor_metadata = os.fstat(descriptor)
        if (
            stat_is_link_or_reparse(path_metadata)
            or not stat.S_ISREG(path_metadata.st_mode)
            or not stat.S_ISREG(descriptor_metadata.st_mode)
            or not _identity_matches(
                lock_path, path_metadata, descriptor, descriptor_metadata
            )
        ):
            raise CacheLockError(f"cache lock is unsafe: {lock_path}")
        # Windows ``msvcrt.locking`` locks a byte range that must lie within the
        # file: locking byte [0, 1) of a 0-byte lock file returns EACCES
        # permanently. Multiple openers can all observe the new file at size
        # zero, however. One may publish and lock the placeholder byte before a
        # peer writes it; that peer's write then receives EACCES. Accept that
        # exact race only after the descriptor proves the byte now exists, then
        # let ``_acquire`` wait for the peer's byte-range lock normally.
        if descriptor_metadata.st_size == 0:
            try:
                os.write(descriptor, b"\0")
            except PermissionError:
                if not _is_windows() or os.fstat(descriptor).st_size == 0:
                    raise
        os.lseek(descriptor, 0, os.SEEK_SET)
        _acquire(descriptor, lock_path, timeout_seconds=timeout_seconds)
        locked = True
        if on_locked is not None:
            on_locked(descriptor)
        _require_safe_ancestors(lock_path.parent)
        observed = lock_path.lstat()
        if stat_is_link_or_reparse(observed) or not _identity_matches(
            lock_path, observed, descriptor, descriptor_metadata
        ):
            raise CacheLockError(f"cache lock changed while acquiring it: {lock_path}")
        yield
    finally:
        if locked:
            _release(descriptor)
        os.close(descriptor)


_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class _Kernel32Windows:
    """Thin wrapper over the Win32 APIs used for reliable Windows file identity.

    ``GetFileInformationByHandle``'s ``nFileIndexHigh``/``nFileIndexLow`` plus the
    volume serial number are Microsoft's documented reliable per-volume file
    identity. Python's ``st_ino``/``st_dev`` are not a stable unique file identity
    across every Windows volume and Python version (issue #76), so this wrapper --
    not a bare ``stat()`` comparison -- backs the Windows TOCTOU swap check.

    Wrapped in a class, and invoked through it rather than inline, so tests on
    non-Windows hosts can substitute a fake without touching ``ctypes.WinDLL``,
    which does not exist off Windows.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes

        class _FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation_time", _FileTime),
                ("last_access_time", _FileTime),
                ("last_write_time", _FileTime),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        self._file_information_struct = _ByHandleFileInformation
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        self._create_file = dll.CreateFileW
        self._create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._create_file.restype = wintypes.HANDLE
        self._close_handle = dll.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL
        self._get_file_information = dll.GetFileInformationByHandle
        self._get_file_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._get_file_information.restype = wintypes.BOOL
        self._invalid_handle = wintypes.HANDLE(-1).value

    def open_reparse_aware(self, path: str) -> int:
        """Open ``path`` without following a trailing symlink or reparse point."""

        handle = self._create_file(
            path,
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if not handle or handle == self._invalid_handle:
            raise OSError(
                self._ctypes.get_last_error(), "cache lock path could not be opened"
            )
        return handle

    def close(self, handle: int) -> None:
        self._close_handle(handle)

    def file_identity(self, handle: int) -> tuple[int, int, int]:
        info = self._file_information_struct()
        if not self._get_file_information(handle, self._ctypes.byref(info)):
            raise OSError(
                self._ctypes.get_last_error(),
                "cache lock file identity inspection failed",
            )
        return (
            int(info.volume_serial_number),
            int(info.file_index_high),
            int(info.file_index_low),
        )


def _default_windows_kernel32() -> _Kernel32Windows:
    return _Kernel32Windows()


def _default_get_osfhandle(descriptor: int) -> int:
    import msvcrt

    return msvcrt.get_osfhandle(descriptor)


def _windows_identity_matches(
    lock_path: Path,
    descriptor: int,
    *,
    kernel32: object | None = None,
    get_osfhandle: Callable[[int], int] | None = None,
) -> bool:
    """Compare path vs. descriptor identity via ``GetFileInformationByHandle``.

    A replaced or reparse-point lock file can make bare ``st_ino`` comparison
    either a no-op (falsely equal) or a flake (spuriously unequal) on Windows.
    Reopening the path without following reparse points and comparing Win32
    file-index/volume identity against the held descriptor's own identity is
    the reliable mechanism for this comparison.
    """

    kernel = kernel32 if kernel32 is not None else _default_windows_kernel32()
    resolve_handle = (
        get_osfhandle if get_osfhandle is not None else _default_get_osfhandle
    )
    try:
        path_handle = kernel.open_reparse_aware(str(lock_path))
        try:
            path_identity = kernel.file_identity(path_handle)
        finally:
            kernel.close(path_handle)
        descriptor_identity = kernel.file_identity(resolve_handle(descriptor))
    except OSError:
        # Fail closed: if identity cannot be verified, treat it as unsafe rather
        # than assuming the path and descriptor still name the same file.
        return False
    return path_identity == descriptor_identity


def _is_windows() -> bool:
    """Indirection over ``os.name`` so tests can select the Windows identity path
    without patching the real ``os.name`` (which pathlib also consults, and which
    breaks ``Path`` construction when patched to ``"nt"`` on a non-Windows host)."""

    return os.name == "nt"


def _identity_matches(
    lock_path: Path,
    path_metadata: os.stat_result,
    descriptor: int,
    descriptor_metadata: os.stat_result,
) -> bool:
    """Return whether ``lock_path`` and the held descriptor name the same file."""

    if _is_windows():
        return _windows_identity_matches(lock_path, descriptor)
    return (path_metadata.st_dev, path_metadata.st_ino) == (
        descriptor_metadata.st_dev,
        descriptor_metadata.st_ino,
    )


def _acquire(
    descriptor: int, path: Path, *, timeout_seconds: float = _LOCK_TIMEOUT_SECONDS
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise CacheLockError(
                    f"cache lock could not be acquired: {path}"
                ) from exc
            if time.monotonic() >= deadline:
                raise CacheLockError(
                    f"cache lock acquisition timed out: {path}"
                ) from exc
            time.sleep(_LOCK_RETRY_SECONDS)


def _release(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _require_safe_ancestors(path: Path) -> None:
    try:
        require_safe_directory(Path(path).absolute())
    except UnsafeFilesystemPathError as exc:
        raise CacheLockError(f"cache lock ancestor is unsafe: {path}") from exc


__all__ = ["CacheLockError", "cache_lock_path", "exclusive_cache_lock"]
