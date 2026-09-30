"""Atomic filesystem storage for target-specific Component locks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    path_is_link_or_reparse,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.component_locking import ComponentAuthoring, ComponentLock
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

LOCK_FILENAME = "component.lock.json"
_WRITE_LOCK_FILENAME = ".component.lock.write.lock"
_OPERATION_LOCK_FILENAME = ".component.operation.write.lock"
_MAX_DIFF_ENTRIES = 512
_MAX_LARGE_DIFF_ENTRIES = _MAX_DIFF_ENTRIES * 64
_MAX_DIFF_TRAVERSAL_ENTRIES = _MAX_DIFF_ENTRIES * 4096
_MAX_PERSISTED_BYTES = 64 * 1024 * 1024


class ComponentLockStoreError(RuntimeError):
    """A lock file is unsafe, malformed, stale, or could not be committed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ComponentLockDifference:
    path: str
    state: str
    current: object | None
    expected: object | None

    def to_dict(self) -> dict[str, object | None]:
        return {
            "path": self.path,
            "state": self.state,
            "current": self.current,
            "expected": self.expected,
        }


@dataclass(frozen=True, slots=True)
class ComponentLockCheck:
    state: str
    path: Path
    expected_identity: str
    current_identity: str | None
    differences: tuple[ComponentLockDifference, ...]
    current_semantically_verified: bool | None

    @property
    def current(self) -> bool:
        return self.state == "current"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/component-lock-check@1",
            "state": self.state,
            "path": str(self.path),
            "expected_identity": self.expected_identity,
            "current_identity": self.current_identity,
            "differences": [item.to_dict() for item in self.differences],
            "current_semantically_verified": self.current_semantically_verified,
        }


@dataclass(frozen=True, slots=True)
class _ComponentLockSnapshot:
    content: bytes | None


class ComponentLockStore:
    """Read, compare, and atomically replace one ``component.lock.json``."""

    def __init__(self, component_root: Path) -> None:
        supplied = Path(component_root)
        try:
            metadata = supplied.lstat()
        except OSError as exc:
            raise ComponentLockStoreError(
                "component_lock.root_unavailable",
                "Component root must be an existing directory",
            ) from exc
        if stat_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ComponentLockStoreError(
                "component_lock.root_unsafe",
                "Component root must be a direct, non-reparse directory",
            )
        self.root = supplied.resolve(strict=True)
        matrix_root = os.environ.get("LITAI_MATRIX_CELL_ROOT")
        if matrix_root:
            storage_root = (
                Path(matrix_root).resolve()
                / "component-locks"
                / hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()
            )
            require_safe_directory(storage_root, allow_missing=True)
        else:
            storage_root = self.root
        self.storage_root = storage_root
        self.path = storage_root / LOCK_FILENAME
        self.write_lock_path = storage_root / _WRITE_LOCK_FILENAME
        self.operation_lock_path = storage_root / _OPERATION_LOCK_FILENAME

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Serialize every lock/audit operation for this Component root."""

        try:
            with exclusive_cache_lock(self.operation_lock_path):
                self._require_root_current()
                yield
                self._require_root_current()
        except ComponentLockStoreError:
            raise
        except CacheLockError as exc:
            raise ComponentLockStoreError(
                "component_lock.operation_lock_failed",
                "Component lock operation could not be serialized",
            ) from exc

    def snapshot(self) -> _ComponentLockSnapshot:
        """Capture bounded bytes for transaction rollback under ``operation``."""

        return _ComponentLockSnapshot(self._read_bytes(required=False))

    def restore_if_current(
        self,
        snapshot: _ComponentLockSnapshot,
        *,
        expected_current: ComponentLock,
    ) -> bool:
        """Restore a snapshot only when this command's candidate is still current."""

        if not isinstance(snapshot, _ComponentLockSnapshot):
            raise TypeError("Component lock rollback requires a store snapshot")
        expected = _lock_bytes(expected_current)
        replacement = snapshot.content
        try:
            with exclusive_cache_lock(self.write_lock_path):
                self._require_root_current()
                if self._read_bytes(required=False) != expected:
                    return False
                if replacement == expected:
                    return False
                if replacement is None:
                    self.path.unlink()
                    _fsync_directory(self.storage_root)
                    return True
                self._replace_bytes(replacement, prefix=f".{LOCK_FILENAME}.rollback.")
                return True
        except ComponentLockStoreError:
            raise
        except (CacheLockError, OSError) as exc:
            raise ComponentLockStoreError(
                "component_lock.rollback_failed",
                "Component lock rollback could not be committed atomically",
            ) from exc

    def read(self, *, authorings: tuple[ComponentAuthoring, ...]) -> ComponentLock:
        content = self._read_bytes(required=True)
        assert content is not None
        try:
            value = json.loads(content)
            lock = ComponentLock.from_dict(value, authorings=authorings)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ComponentLockStoreError(
                "component_lock.invalid", "Component lock is malformed or forged"
            ) from exc
        if content != _lock_bytes(lock):
            raise ComponentLockStoreError(
                "component_lock.noncanonical",
                "Component lock does not use canonical bytes",
            )
        return lock

    def read_from_catalog(
        self, *, authorings: tuple[ComponentAuthoring, ...]
    ) -> ComponentLock:
        """Read canonical bytes using only authorings selected by the lock itself."""

        content = self._read_bytes(required=True)
        assert content is not None
        try:
            value = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ComponentLockStoreError(
                "component_lock.invalid", "Component lock is malformed or forged"
            ) from exc
        if not isinstance(value, dict) or value.get("schema") != ComponentLock.SCHEMA:
            raise ComponentLockStoreError(
                "component_lock.invalid", "Component lock is malformed or forged"
            )
        if content != canonical_json_bytes(value) + b"\n":
            raise ComponentLockStoreError(
                "component_lock.noncanonical",
                "Component lock does not use canonical bytes",
            )
        try:
            selected_identities = _locked_authoring_identities(value)
        except (TypeError, ValueError) as exc:
            raise ComponentLockStoreError(
                "component_lock.invalid", "Component lock is malformed or forged"
            ) from exc
        by_identity = {item.identity.uri: item for item in authorings}
        if len(by_identity) != len(authorings):
            raise ComponentLockStoreError(
                "component_lock.catalog_ambiguous",
                "Component catalog repeats an authoring identity",
            )
        if any(identity.uri not in by_identity for identity in selected_identities):
            raise ComponentLockStoreError(
                "component_lock.stale",
                "Component lock selects authoring absent from the current catalog",
            )
        selected_authorings = tuple(
            sorted(
                (by_identity[identity.uri] for identity in selected_identities),
                key=lambda item: item.identity.uri,
            )
        )
        try:
            return ComponentLock.from_dict(value, authorings=selected_authorings)
        except (TypeError, ValueError) as exc:
            raise ComponentLockStoreError(
                "component_lock.invalid", "Component lock is malformed or forged"
            ) from exc

    def require_current(self, expected: ComponentLock) -> None:
        """Require this exact store to retain one lock's canonical bytes."""

        if self._read_bytes(required=True) != _lock_bytes(expected):
            raise ComponentLockStoreError(
                "component_lock.changed_during_lifecycle",
                "the selected Component lock changed during the lifecycle",
            )

    def check(
        self,
        expected: ComponentLock,
        *,
        authorings: tuple[ComponentAuthoring, ...],
    ) -> ComponentLockCheck:
        return self._check(
            expected,
            authorings=authorings,
            difference_limit=_MAX_DIFF_ENTRIES,
        )

    def review(
        self,
        expected: ComponentLock,
        *,
        authorings: tuple[ComponentAuthoring, ...],
    ) -> ComponentLockCheck:
        """Return a bounded complete diff for an explicit large-review transaction."""

        return self._check(
            expected,
            authorings=authorings,
            difference_limit=_MAX_LARGE_DIFF_ENTRIES,
        )

    def _check(
        self,
        expected: ComponentLock,
        *,
        authorings: tuple[ComponentAuthoring, ...],
        difference_limit: int,
    ) -> ComponentLockCheck:
        current_bytes = self._read_bytes(required=False)
        if current_bytes is None:
            return ComponentLockCheck(
                "missing",
                self.path,
                expected.identity.uri,
                None,
                (
                    ComponentLockDifference(
                        "/",
                        "missing",
                        None,
                        {
                            "schema": ComponentLock.SCHEMA,
                            "identity": expected.identity.uri,
                        },
                    ),
                ),
                None,
            )
        try:
            raw = json.loads(current_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ComponentLockStoreError(
                "component_lock.invalid", "Component lock is malformed or forged"
            ) from exc
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != ComponentLock.SCHEMA
            or current_bytes != canonical_json_bytes(raw) + b"\n"
        ):
            raise ComponentLockStoreError(
                "component_lock.invalid", "Component lock is malformed or forged"
            )
        try:
            current = ComponentLock.from_dict(raw, authorings=authorings)
        except (TypeError, ValueError):
            current = None
        current_identity = canonical_identity(raw)
        if current is None:
            if current_identity == expected.identity:
                raise ComponentLockStoreError(
                    "component_lock.invalid",
                    "Component lock claims the expected identity but fails validation",
                )
            return ComponentLockCheck(
                "stale",
                self.path,
                expected.identity.uri,
                current_identity.uri,
                _semantic_diff(
                    raw,
                    expected.to_dict(),
                    maximum_differences=difference_limit,
                ),
                False,
            )
        if current.identity == expected.identity:
            if current_bytes != _lock_bytes(expected):
                raise ComponentLockStoreError(
                    "component_lock.noncanonical",
                    "Component lock has the expected meaning but non-canonical bytes",
                )
            return ComponentLockCheck(
                "current",
                self.path,
                expected.identity.uri,
                current.identity.uri,
                (),
                True,
            )
        return ComponentLockCheck(
            "stale",
            self.path,
            expected.identity.uri,
            current.identity.uri,
            _semantic_diff(
                current.to_dict(),
                expected.to_dict(),
                maximum_differences=difference_limit,
            ),
            True,
        )

    def update(
        self,
        candidate: ComponentLock,
        *,
        revalidate: Callable[[], None] | None = None,
    ) -> bool:
        """Replace the lock atomically after a final caller-owned input check."""

        content = _lock_bytes(candidate)
        if len(content) > _MAX_PERSISTED_BYTES:
            raise ComponentLockStoreError(
                "component_lock.limit_exceeded",
                "Component lock exceeds the persisted byte limit",
            )
        try:
            with exclusive_cache_lock(self.write_lock_path):
                self._require_root_current()
                current = self._read_bytes(required=False)
                if current == content:
                    if revalidate is not None:
                        revalidate()
                    return False
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{LOCK_FILENAME}.", dir=self.storage_root
                )
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if revalidate is not None:
                        revalidate()
                    self._require_root_current()
                    if self._read_bytes(required=False) != current:
                        raise ComponentLockStoreError(
                            "component_lock.concurrent_change",
                            "Component lock changed while its replacement was prepared",
                        )
                    if path_is_link_or_reparse(self.path):
                        raise ComponentLockStoreError(
                            "component_lock.path_unsafe",
                            "Component lock destination cannot be a link or "
                            "reparse point",
                        )
                    if self.path.exists() and not self.path.is_file():
                        raise ComponentLockStoreError(
                            "component_lock.path_unsafe",
                            "Component lock destination must be a regular file",
                        )
                    os.replace(temporary, self.path)
                    _fsync_directory(self.storage_root)
                finally:
                    temporary.unlink(missing_ok=True)
        except ComponentLockStoreError:
            raise
        except (CacheLockError, OSError) as exc:
            raise ComponentLockStoreError(
                "component_lock.write_failed",
                "Component lock could not be committed atomically",
            ) from exc
        return True

    def _read_bytes(self, *, required: bool) -> bytes | None:
        self._require_root_current()
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            if required:
                raise ComponentLockStoreError(
                    "component_lock.missing", "Component lock does not exist"
                ) from None
            return None
        except OSError as exc:
            raise ComponentLockStoreError(
                "component_lock.read_failed", "Component lock could not be inspected"
            ) from exc
        if stat_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ComponentLockStoreError(
                "component_lock.path_unsafe",
                "Component lock destination must be a regular file",
            )
        if metadata.st_size > _MAX_PERSISTED_BYTES:
            raise ComponentLockStoreError(
                "component_lock.limit_exceeded",
                "Component lock exceeds the persisted byte limit",
            )
        try:
            with self.path.open("rb") as stream:
                content = stream.read(_MAX_PERSISTED_BYTES + 1)
            after = self.path.lstat()
        except OSError as exc:
            raise ComponentLockStoreError(
                "component_lock.read_failed", "Component lock could not be read"
            ) from exc
        if (
            stat_is_link_or_reparse(after)
            or not stat.S_ISREG(after.st_mode)
            or _node_signature(metadata) != _node_signature(after)
        ):
            raise ComponentLockStoreError(
                "component_lock.concurrent_change",
                "Component lock changed while it was read",
            )
        if len(content) > _MAX_PERSISTED_BYTES:
            raise ComponentLockStoreError(
                "component_lock.limit_exceeded",
                "Component lock exceeds the persisted byte limit",
            )
        return content

    def _replace_bytes(self, content: bytes, *, prefix: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=prefix, dir=self.storage_root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if path_is_link_or_reparse(self.path):
                raise ComponentLockStoreError(
                    "component_lock.path_unsafe",
                    "Component lock destination cannot be a link or reparse point",
                )
            if self.path.exists() and not self.path.is_file():
                raise ComponentLockStoreError(
                    "component_lock.path_unsafe",
                    "Component lock destination must be a regular file",
                )
            os.replace(temporary, self.path)
            _fsync_directory(self.storage_root)
        finally:
            temporary.unlink(missing_ok=True)

    def _require_root_current(self) -> None:
        try:
            require_safe_directory(self.root)
            require_safe_directory(self.storage_root, allow_missing=True)
        except UnsafeFilesystemPathError as exc:
            raise ComponentLockStoreError(
                "component_lock.root_unsafe", "Component root became unsafe"
            ) from exc


def _locked_authoring_identities(
    value: Mapping[str, object],
) -> tuple[ContentIdentity, ...]:
    nodes = value.get("nodes")
    if (
        not isinstance(nodes, Sequence)
        or isinstance(nodes, (str, bytes, bytearray))
        or not nodes
        or len(nodes) > 4096
    ):
        raise ValueError("Component lock nodes are invalid")
    identities: list[ContentIdentity] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise ValueError("Component lock node is invalid")
        revision = node.get("revision")
        if not isinstance(revision, Mapping):
            raise ValueError("Component lock revision is invalid")
        identities.append(
            ContentIdentity.from_dict(
                revision.get("authoring_identity"),
                path=f"ComponentLock.nodes[{index}].revision.authoring_identity",
            )
        )
    if len({item.uri for item in identities}) != len(identities):
        raise ValueError("Component lock repeats an authoring identity")
    return tuple(identities)


def _lock_bytes(lock: ComponentLock) -> bytes:
    return canonical_json_bytes(lock.to_dict()) + b"\n"


def _semantic_diff(
    current: object,
    expected: object,
    *,
    path: str = "",
    maximum_differences: int = _MAX_DIFF_ENTRIES,
) -> tuple[ComponentLockDifference, ...]:
    if (
        not isinstance(maximum_differences, int)
        or isinstance(maximum_differences, bool)
        or maximum_differences < 1
        or maximum_differences > _MAX_LARGE_DIFF_ENTRIES
    ):
        raise ValueError("Component lock difference limit is invalid")
    differences: list[ComponentLockDifference] = []
    pending: list[tuple[str, object, object]] = [(path, current, expected)]
    traversed_entries = 0

    def record(difference: ComponentLockDifference) -> None:
        differences.append(difference)
        if len(differences) > maximum_differences:
            raise ComponentLockStoreError(
                "component_lock.diff_limit",
                "Component lock difference exceeds the bounded review limit",
            )

    def account_traversal(entries: int) -> None:
        nonlocal traversed_entries
        traversed_entries += entries
        if traversed_entries > _MAX_DIFF_TRAVERSAL_ENTRIES:
            raise ComponentLockStoreError(
                "component_lock.diff_traversal_limit",
                "Component lock comparison exceeds the bounded traversal limit",
            )

    while pending:
        account_traversal(1)
        item_path, left, right = pending.pop()
        if left == right:
            continue
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            keys = sorted(set(left) | set(right), reverse=True)
            account_traversal(len(keys))
            for key in keys:
                child = f"{item_path}/{_json_pointer_token(str(key))}"
                if key not in left:
                    record(ComponentLockDifference(child, "added", None, right[key]))
                elif key not in right:
                    record(ComponentLockDifference(child, "removed", left[key], None))
                elif left[key] != right[key]:
                    pending.append((child, left[key], right[key]))
        elif (
            isinstance(left, Sequence)
            and not isinstance(left, (str, bytes, bytearray))
            and isinstance(right, Sequence)
            and not isinstance(right, (str, bytes, bytearray))
        ):
            maximum = max(len(left), len(right))
            account_traversal(maximum)
            for index in reversed(range(maximum)):
                child = f"{item_path}/{index}"
                if index >= len(left):
                    record(ComponentLockDifference(child, "added", None, right[index]))
                elif index >= len(right):
                    record(ComponentLockDifference(child, "removed", left[index], None))
                elif left[index] != right[index]:
                    pending.append((child, left[index], right[index]))
        else:
            record(ComponentLockDifference(item_path or "/", "changed", left, right))
    return tuple(sorted(differences, key=lambda item: item.path))


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _node_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


__all__ = [
    "ComponentLockCheck",
    "ComponentLockDifference",
    "ComponentLockStore",
    "ComponentLockStoreError",
    "LOCK_FILENAME",
]
