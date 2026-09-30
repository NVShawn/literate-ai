"""Exclusive, process-owned write markers with conservative cleanup.

Markers survive crashes and require explicit recovery. They are cooperative
reservations, not containment of a hostile process running as the same user.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from literate_ai._cache_lock import CacheLockError, _acquire, _identity_matches
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.paths import canonical_relative_posix_path

from .repository_orchestration import OrchestrationInventoryError, _read_document

_CREATION_KEY = object()


def _fail(suffix: str, message: str) -> None:
    raise OrchestrationInventoryError("reservation_" + suffix, message) from None


def is_reservation_cleanup_error(error: BaseException) -> bool:
    return (
        isinstance(error, OrchestrationInventoryError)
        and error.code == "orchestration.reservation_cleanup_incomplete"
    )


def combine_body_and_release_errors(
    boundary: str,
    body_error: BaseException,
    body_traceback: TracebackType | None,
    release_error: BaseException,
) -> BaseExceptionGroup:
    return BaseExceptionGroup(
        f"repository refresh body and {boundary} release both failed",
        (body_error.with_traceback(body_traceback), release_error),
    )


def _key(node: os.stat_result) -> tuple[int, int, int]:
    return node.st_dev, node.st_ino, node.st_mode


@dataclass(frozen=True, slots=True)
class WriteReservationTarget:
    path: Path
    anchor: Path
    anchor_node: tuple[int, int, int]
    advisory: bool = False

    def __post_init__(self) -> None:
        if type(self.advisory) is not bool:
            raise TypeError("reservation advisory mode must be boolean")
        if not self.path.is_absolute() or not self.anchor.is_absolute():
            raise ValueError("write reservation paths must be absolute")
        canonical_relative_posix_path(
            self.path.relative_to(self.anchor).as_posix(), label="write reservation"
        )


@dataclass(slots=True)
class _OwnedMarker:
    target: WriteReservationTarget
    descriptor: int
    node: tuple[int, int, int]
    content: bytes = b""
    created: bool = True
    advisory_locked: bool = False


class WriteReservationSet:
    """Live descriptor-backed ownership; not constructible from a saved document."""

    def __init__(self, key: object):
        if key is not _CREATION_KEY:
            raise TypeError("reservations must be acquired, not reconstructed")
        self._markers: list[_OwnedMarker] = []
        self._directories: list[
            tuple[Path, tuple[int, int, int], WriteReservationTarget]
        ] = []
        self._active = True
        self._complete = False

    @staticmethod
    def _guard(target: WriteReservationTarget) -> None:
        require_safe_directory(target.anchor)
        if _key(target.anchor.lstat()) != target.anchor_node:
            _fail("changed", "write reservation anchor changed")

    @staticmethod
    def _no_alias(path: Path) -> None:
        for index, entry in enumerate(path.parent.iterdir()):
            if index >= 16384:
                _fail("limit", "write reservation directory exceeds its entry bound")
            if (
                entry.name != path.name
                and entry.name.casefold() == path.name.casefold()
            ):
                _fail("alias", "write reservation has a portable path alias")

    def _parents(self, target: WriteReservationTarget) -> None:
        parent = target.anchor
        for part in target.path.relative_to(target.anchor).parts[:-1]:
            self._guard(target)
            require_safe_directory(parent)
            parent = parent / part
            self._no_alias(parent)
            try:
                parent.mkdir(mode=0o700)
            except FileExistsError:
                require_safe_directory(parent)
            else:
                self._directories.append((parent, _key(parent.lstat()), target))
        require_safe_directory(target.path.parent)

    def _matches(self, marker: _OwnedMarker) -> bool:
        target = marker.target
        self._guard(target)
        require_safe_directory(target.path.parent)
        for path, node, _ in self._directories:
            if target.path.is_relative_to(path) and _key(path.lstat()) != node:
                return False
        self._no_alias(target.path)
        named = target.path.lstat()
        if (
            stat_is_link_or_reparse(named)
            or not stat.S_ISREG(named.st_mode)
            or _key(named) != marker.node
            or named.st_nlink != 1
        ):
            return False
        if marker.descriptor < 0:
            return _read_document(target.path, maximum_bytes=256) == marker.content
        opened = os.fstat(marker.descriptor)
        if not _identity_matches(target.path, named, marker.descriptor, opened):
            return False
        os.lseek(marker.descriptor, 0, os.SEEK_SET)
        content = os.read(marker.descriptor, len(marker.content) + 1)
        return content == marker.content and _identity_matches(
            target.path,
            target.path.lstat(),
            marker.descriptor,
            os.fstat(marker.descriptor),
        )

    def _acquire(self, target: WriteReservationTarget) -> None:
        self._guard(target)
        self._parents(target)
        self._no_alias(target.path)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(target.path, flags, 0o600)
        except FileExistsError:
            if not target.advisory:
                _fail(
                    "busy", "write reservation already exists; retain it for its owner"
                )
            # Existing advisory files are not ours to rewrite or retire. Capture
            # bounded bytes before opening; descriptor identity is checked below.
            content = _read_document(target.path, maximum_bytes=256)
            if content.startswith(b"literate-ai-refresh-reservation\n"):
                _fail("busy", "retained refresh reservation requires explicit recovery")
            if not content:
                _fail("empty", "retained advisory lock has no lockable placeholder")
            node = _key(target.path.lstat())
            descriptor = os.open(target.path, flags & ~(os.O_CREAT | os.O_EXCL))
            marker = _OwnedMarker(target, descriptor, node, content, created=False)
            self._markers.append(marker)
            if not self._matches(marker):
                _fail("changed", "advisory lock changed during acquisition")
            _acquire(descriptor, target.path, timeout_seconds=0.0)
            marker.advisory_locked = True
            if not self._matches(marker):
                _fail("changed", "advisory lock changed while acquiring ownership")
            return
        marker = _OwnedMarker(target, descriptor, (0, 0, 0))
        self._markers.append(marker)
        marker.node = _key(target.path.lstat())
        if not self._matches(marker):
            _fail("changed", "write reservation changed during acquisition")
        token = (
            b"literate-ai-refresh-reservation\n"
            + secrets.token_hex(32).encode()
            + b"\n"
        )
        while len(marker.content) < len(token):
            count = os.write(descriptor, token[len(marker.content) :])
            if count <= 0:
                _fail("write_failed", "write reservation token made no progress")
            marker.content = token[: len(marker.content) + count]
        os.fsync(descriptor)
        if not self._matches(marker):
            _fail("changed", "write reservation changed while recording ownership")
        if target.advisory:
            _acquire(descriptor, target.path, timeout_seconds=0.0)
            marker.advisory_locked = True

    def owns(self, path: Path) -> bool:
        if not self._active or not self._complete:
            _fail("inactive", "write reservations are not live and complete")
        for marker in self._markers:
            if marker.target.path == path:
                if not self._matches(marker):
                    _fail("changed", "write reservation ownership changed")
                return True
        return False

    def owns_directory(self, path: Path) -> bool:
        """Prove a live directory was created by this reservation owner."""
        if not self._active or not self._complete:
            _fail("inactive", "write reservations are not live and complete")
        for owned, node, target in self._directories:
            if owned == path:
                self._guard(target)
                require_safe_directory(path)
                if _key(path.lstat()) != node:
                    _fail("changed", "owned reservation directory changed")
                return True
        return False

    def verify_all(self) -> None:
        if not self._active or not self._complete:
            _fail("inactive", "write reservations are not live and complete")
        try:
            if not all(self._matches(marker) for marker in self._markers):
                _fail("changed", "write reservation ownership changed")
            for path, _, _ in self._directories:
                self.owns_directory(path)
        except (OSError, ValueError, UnsafeFilesystemPathError):
            _fail("changed", "write reservation ownership is unavailable or changed")

    def prepare_committed_directory_transfer(
        self, paths: tuple[Path, ...]
    ) -> tuple[tuple[Path, tuple[int, int, int], WriteReservationTarget], ...]:
        """Validate a later non-fallible committed-directory ownership transfer."""
        if not isinstance(paths, tuple) or any(
            not isinstance(path, Path) or not path.is_absolute() for path in paths
        ):
            raise TypeError("committed directory transfer requires absolute paths")
        self.verify_all()
        transferred = []
        for directory, node, target in self._directories:
            if any(
                path == directory or path.is_relative_to(directory) for path in paths
            ):
                self._guard(target)
                require_safe_directory(directory)
                if _key(directory.lstat()) != node:
                    _fail("changed", "committed parent directory changed")
                transferred.append((directory, node, target))
        self.verify_all()
        return tuple(transferred)

    def commit_directory_transfer(
        self,
        prepared: tuple[tuple[Path, tuple[int, int, int], WriteReservationTarget], ...],
    ) -> None:
        """Publish a prevalidated transfer using only in-memory ownership state."""
        selected = set(prepared)
        if any(item not in self._directories for item in prepared):
            raise RuntimeError("prepared committed directory transfer changed")
        self._directories = [item for item in self._directories if item not in selected]

    def transfer_committed_directories(self, paths: tuple[Path, ...]) -> None:
        """Validate and transfer owner-created committed parent directories."""
        prepared = self.prepare_committed_directory_transfer(paths)
        self.commit_directory_transfer(prepared)

    def _release(self) -> None:
        self._active = False
        retained = False
        for marker in reversed(self._markers):
            removed = False
            try:
                current = self._matches(marker)
                if (
                    marker.created
                    and marker.target.advisory
                    and not marker.advisory_locked
                ):
                    current = False
                # POSIX advisory waiters must never acquire a pathname whose
                # inode we unlink after releasing it. Their existing lock
                # protocol rechecks identity when the held inode is unlinked.
                if (
                    current
                    and marker.created
                    and marker.target.advisory
                    and os.name != "nt"
                ):
                    marker.target.path.unlink()
                    removed = True
            except (OSError, ValueError, UnsafeFilesystemPathError):
                current = False
            finally:
                try:
                    os.close(marker.descriptor)
                except OSError:
                    retained = True
                marker.descriptor = -1
            if not marker.created or removed:
                retained |= not current
                continue
            try:
                # Reopen only after closing our descriptor (Windows unlink rules).
                if current and self._matches(marker):
                    marker.target.path.unlink()
                else:
                    retained = True
            except (OSError, ValueError, UnsafeFilesystemPathError):
                retained = True
        for path, node, target in reversed(self._directories):
            try:
                self._guard(target)
                require_safe_directory(path)
                if _key(path.lstat()) != node:
                    retained = True
                else:
                    path.rmdir()  # Never remove a concurrent owner's contents.
            except (OSError, ValueError, UnsafeFilesystemPathError):
                retained = True
        self._active = False
        if retained:
            _fail(
                "cleanup_incomplete",
                "changed reservations or parent directories were retained",
            )


@contextmanager
def acquire_write_reservations(
    targets: tuple[WriteReservationTarget, ...],
) -> Iterator[WriteReservationSet]:
    if not isinstance(targets, tuple) or not 1 <= len(targets) <= 1024:
        raise ValueError("write reservations require a bounded immutable target set")
    # WindowsPath equality folds case; preserve spelling until alias validation.
    unique: dict[str, WriteReservationTarget] = {}
    aliases: set[str] = set()
    for target in targets:
        if not isinstance(target, WriteReservationTarget):
            raise TypeError("write reservations require typed targets")
        spelling = str(target.path)
        if spelling in unique:
            previous = unique[spelling]
            if previous != target or str(previous.anchor) != str(target.anchor):
                _fail("alias", "one write path has conflicting reservation anchors")
            continue
        alias = spelling.casefold()
        if alias in aliases:
            _fail("alias", "write reservation targets alias")
        aliases.add(alias)
        unique[spelling] = target
    owned = WriteReservationSet(_CREATION_KEY)
    try:
        try:
            for path in sorted(unique, key=str):
                owned._acquire(unique[path])
            owned._complete = True
            owned.verify_all()
        except OrchestrationInventoryError:
            raise
        except CacheLockError:
            _fail("busy", "framework advisory lock is held or unavailable")
        except (OSError, UnsafeFilesystemPathError):
            _fail("write_failed", "write reservations could not be acquired safely")
        # Execution failures belong to the caller, not reservation acquisition.
        yield owned
        owned.verify_all()
    finally:
        owned._release()
