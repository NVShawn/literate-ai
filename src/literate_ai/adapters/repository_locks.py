"""Bounded root lock custody; no child authority or Git pin mutation.

Updates serialize with an exclusive marker, not a persistent daemon. A crash may
leave that marker for owner inspection. Reobservation detects concurrent changes;
it is not containment against another process running as the same user.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.repository_lock import RepositoryLock

from .repository_lock_planning import (
    PreparedRepositoryLock,
    require_repository_lock_inputs_unchanged,
)
from .repository_orchestration import OrchestrationInventoryError, _read_document

REPOSITORY_LOCK_PATH = ".literate/repository.lock.json"
MAXIMUM_LOCK_BYTES = 16 * 1024 * 1024
_WRITER_NAME = ".repository-lock.write"


def _fail(suffix: str, message: str) -> None:
    raise OrchestrationInventoryError(suffix, message)


def _key(node: os.stat_result) -> tuple[int, int, int]:
    return node.st_dev, node.st_ino, node.st_mode


def _signature(node: os.stat_result) -> tuple[int, ...]:
    return (*_key(node), node.st_size, node.st_mtime_ns, node.st_ctime_ns)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    content: bytes | None
    signature: tuple[int, ...] | None
    lock: RepositoryLock | None


@dataclass(slots=True)
class _OwnedFile:
    path: Path
    key: tuple[int, int, int]
    content: bytes = b""


class RepositoryLockStore:
    """Read/check without writes; replace only an unchanged, valid prior lock."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()
        self.directory = self.root / ".literate"
        self.path = self.root / REPOSITORY_LOCK_PATH
        self.writer_path = self.directory / _WRITER_NAME
        try:
            require_safe_directory(self.directory)
            self.root_key = _key(self.root.lstat())
            self.directory_key = _key(self.directory.lstat())
            self._guard()
        except (OSError, UnsafeFilesystemPathError) as exc:
            raise OrchestrationInventoryError(
                "lock_path_unsafe", "repository lock requires direct root metadata"
            ) from exc

    def _no_alias(self, path: Path) -> None:
        if any(
            entry.name != path.name and entry.name.casefold() == path.name.casefold()
            for entry in path.parent.iterdir()
        ):
            _fail("lock_path_unsafe", "repository lock path has a portable alias")

    def _guard(self) -> None:
        try:
            require_safe_directory(self.directory)
            if (
                _key(self.root.lstat()) != self.root_key
                or _key(self.directory.lstat()) != self.directory_key
            ):
                _fail("lock_path_changed", "repository lock directory identity changed")
            self._no_alias(self.directory)
            self._no_alias(self.path)
            self._no_alias(self.writer_path)
        except (OSError, UnsafeFilesystemPathError) as exc:
            raise OrchestrationInventoryError(
                "lock_path_unsafe",
                "repository lock directory is unavailable or indirect",
            ) from exc

    def _read_bytes(self, path: Path) -> bytes:
        return _read_document(
            path,
            label="repository lock input",
            error_suffix="lock_invalid",
            maximum_bytes=MAXIMUM_LOCK_BYTES,
        )

    def _snapshot(self) -> _Snapshot:
        self._guard()
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            return _Snapshot(None, None, None)
        try:
            content = self._read_bytes(self.path)
            after = self.path.lstat()
            self._guard()
            if _signature(before) != _signature(after):
                _fail("lock_changed", "repository lock changed during observation")
            value = json.loads(content)
            lock = RepositoryLock.from_dict(value)
            if content != canonical_json_bytes(lock.to_dict()) + b"\n":
                _fail("lock_invalid", "repository lock bytes are not canonical")
            return _Snapshot(content, _signature(after), lock)
        except OrchestrationInventoryError:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            UnsafeFilesystemPathError,
        ) as exc:
            raise OrchestrationInventoryError(
                "lock_invalid", "repository lock is unavailable, malformed or unsafe"
            ) from exc

    def read(self) -> RepositoryLock | None:
        first = self._snapshot()
        if self._snapshot() != first:
            _fail("lock_changed", "repository lock changed between observations")
        return first.lock

    def check(self, expected: RepositoryLock) -> dict[str, Any]:
        if not isinstance(expected, RepositoryLock):
            raise TypeError("repository lock check requires a typed candidate")
        current = self.read()
        return {
            "schema": "literate-ai/repository-lock-check@1",
            "path": REPOSITORY_LOCK_PATH,
            "state": "missing"
            if current is None
            else "current"
            if current == expected
            else "stale",
            "expected_identity": expected.identity,
            "current_identity": None if current is None else current.identity,
        }

    def _owned_current(self, owned: _OwnedFile) -> bool:
        self._guard()
        return (
            _key(owned.path.lstat()) == owned.key
            and self._read_bytes(owned.path) == owned.content
        )

    def _cleanup(self, owned: _OwnedFile) -> bool:
        try:
            if not self._owned_current(owned):
                return False
            owned.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except (OSError, ValueError, UnsafeFilesystemPathError):
            return False

    def _stage(self, content: bytes, owned_files: list[_OwnedFile]) -> _OwnedFile:
        self._guard()
        descriptor, name = tempfile.mkstemp(
            prefix=".repository-lock-", suffix=".tmp", dir=self.directory
        )
        try:
            owned = _OwnedFile(Path(name), _key(os.fstat(descriptor)))
            owned_files.append(owned)
            while len(owned.content) < len(content):
                count = os.write(descriptor, content[len(owned.content) :])
                if count <= 0:
                    raise OSError("repository lock staging made no progress")
                owned.content = content[: len(owned.content) + count]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if not self._owned_current(owned):
            _fail("lock_changed", "staged repository lock changed before publication")
        return owned

    def update(self, prepared: PreparedRepositoryLock) -> bool:
        """Publish one prepared lock, retaining unqualified output on late drift.

        This operation never updates the root manifest or Gitlinks. A failure after
        publication is explicit; consumers must still check current inputs and locks.
        """
        if not isinstance(prepared, PreparedRepositoryLock):
            raise TypeError("repository lock update requires prepared root authority")
        if prepared.root != self.root or prepared.root_node != self.root_key[:2]:
            _fail(
                "lock_root_mismatch", "prepared repository lock belongs to another root"
            )
        content = canonical_json_bytes(prepared.lock.to_dict()) + b"\n"
        if len(content) > MAXIMUM_LOCK_BYTES:
            _fail("lock_limit_exceeded", "repository lock exceeds its byte limit")
        require_repository_lock_inputs_unchanged(prepared)
        self._guard()
        before = self._snapshot()
        if before.content == content:
            require_repository_lock_inputs_unchanged(prepared)
            if self._snapshot() != before:
                _fail("lock_changed", "repository lock changed during no-op update")
            return False
        owned_files: list[_OwnedFile] = []
        published = False
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.writer_path, flags, 0o600)
            except FileExistsError as exc:
                raise OrchestrationInventoryError(
                    "lock_busy",
                    "repository lock writer marker exists; "
                    "inspect its owner before retrying",
                ) from exc
            try:
                writer = _OwnedFile(self.writer_path, _key(os.fstat(descriptor)))
                owned_files.append(writer)
            finally:
                os.close(descriptor)
            self._guard()
            require_repository_lock_inputs_unchanged(prepared)
            if self._snapshot() != before:
                _fail(
                    "lock_changed",
                    "repository lock changed before acquiring publication custody",
                )
            staged = self._stage(content, owned_files)
            require_repository_lock_inputs_unchanged(prepared)
            if (
                not self._owned_current(writer)
                or not self._owned_current(staged)
                or self._snapshot() != before
            ):
                _fail("lock_changed", "repository lock publication inputs changed")
            if before.content is None:
                os.link(staged.path, self.path, follow_symlinks=False)
            else:
                os.replace(staged.path, self.path)
            published = True
            if os.name != "nt":
                descriptor = os.open(
                    self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._guard()
            if not self._owned_current(_OwnedFile(self.path, staged.key, content)):
                _fail("lock_changed", "published repository lock bytes changed")
            require_repository_lock_inputs_unchanged(prepared)
            final = self._snapshot()
            if final.content != content or final.signature[:3] != staged.key:
                _fail("lock_changed", "repository lock changed after publication")
            return True
        except Exception as exc:
            if published:
                raise OrchestrationInventoryError(
                    "lock_postpublication_failed",
                    "repository lock was published but final qualification failed; "
                    "inspect and recheck it",
                ) from exc
            if isinstance(exc, OrchestrationInventoryError):
                raise
            raise OrchestrationInventoryError(
                "lock_write_failed",
                "repository lock publication did not complete; "
                "inspect current state before retrying",
            ) from exc
        finally:
            retained = [
                owned for owned in reversed(owned_files) if not self._cleanup(owned)
            ]
            if retained:
                _fail(
                    "lock_cleanup_incomplete",
                    "repository lock staging or writer marker was retained; "
                    "inspect it before retrying",
                )
