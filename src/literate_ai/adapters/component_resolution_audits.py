"""Target-scoped filesystem storage for non-authoritative resolution audits."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    path_is_link_or_reparse,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.component_locking import ComponentResolutionAudit
from literate_ai.contracts.identity import canonical_json_bytes

_PORTABLE_TARGET = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_AUDIT_PREFIX = "component.resolution-audit."
_AUDIT_SUFFIX = ".json"
_MAX_PERSISTED_BYTES = 64 * 1024 * 1024


class ComponentResolutionAuditStoreError(RuntimeError):
    """An audit file is unsafe, malformed, noncanonical, stale, or unwritable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ComponentResolutionAuditCheck:
    state: str
    path: Path
    expected_identity: str
    current_identity: str | None

    @property
    def current(self) -> bool:
        return self.state == "current"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/component-resolution-audit-check@1",
            "state": self.state,
            "path": str(self.path),
            "expected_identity": self.expected_identity,
            "current_identity": self.current_identity,
        }


@dataclass(frozen=True, slots=True)
class _ComponentResolutionAuditSnapshot:
    content: bytes | None


class ComponentResolutionAuditStore:
    """Read, check, and atomically replace one target's catalog audit evidence."""

    def __init__(self, component_root: Path, target_name: str) -> None:
        if (
            not isinstance(target_name, str)
            or _PORTABLE_TARGET.fullmatch(target_name) is None
        ):
            raise ValueError("resolution-audit target name must be portable")
        supplied = Path(component_root)
        try:
            metadata = supplied.lstat()
        except OSError as exc:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.root_unavailable",
                "Component root must be an existing directory",
            ) from exc
        if stat_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.root_unsafe",
                "Component root must be a direct, non-reparse directory",
            )
        self.root = supplied.resolve(strict=True)
        matrix_root = os.environ.get("LITAI_MATRIX_CELL_ROOT")
        if matrix_root:
            storage_root = (
                Path(matrix_root).resolve()
                / "resolution-audits"
                / hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()
            )
            require_safe_directory(storage_root, allow_missing=True)
        else:
            storage_root = self.root
        self.storage_root = storage_root
        self.target_name = target_name
        self.path = storage_root / f"{_AUDIT_PREFIX}{target_name}{_AUDIT_SUFFIX}"
        self.write_lock_path = storage_root / f".{self.path.name}.write.lock"

    def read(self) -> ComponentResolutionAudit:
        content = self._read_bytes(required=True)
        assert content is not None
        return self._parse_canonical(content)

    def snapshot(self) -> _ComponentResolutionAuditSnapshot:
        """Capture bounded bytes for pair-publication rollback."""

        return _ComponentResolutionAuditSnapshot(self._read_bytes(required=False))

    def restore_if_current(
        self,
        snapshot: _ComponentResolutionAuditSnapshot,
        *,
        expected_current: ComponentResolutionAudit,
    ) -> bool:
        """Restore a snapshot only while this command's audit remains current."""

        if not isinstance(snapshot, _ComponentResolutionAuditSnapshot):
            raise TypeError("resolution-audit rollback requires a store snapshot")
        if not isinstance(expected_current, ComponentResolutionAudit):
            raise TypeError("resolution-audit rollback requires typed current evidence")
        expected = _audit_bytes(expected_current)
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
                self._replace_bytes(replacement, prefix=f".{self.path.name}.rollback.")
                return True
        except ComponentResolutionAuditStoreError:
            raise
        except (CacheLockError, OSError) as exc:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.rollback_failed",
                "resolution-audit rollback could not be committed atomically",
            ) from exc

    def check(
        self, expected: ComponentResolutionAudit
    ) -> ComponentResolutionAuditCheck:
        if not isinstance(expected, ComponentResolutionAudit):
            raise TypeError("resolution-audit check requires typed expected evidence")
        current_bytes = self._read_bytes(required=False)
        if current_bytes is None:
            return ComponentResolutionAuditCheck(
                "missing", self.path, expected.identity.uri, None
            )
        current = self._parse_canonical(current_bytes)
        return ComponentResolutionAuditCheck(
            "current" if current.identity == expected.identity else "stale",
            self.path,
            expected.identity.uri,
            current.identity.uri,
        )

    def update(
        self,
        candidate: ComponentResolutionAudit,
        *,
        revalidate: Callable[[], None] | None = None,
    ) -> bool:
        """Atomically replace this target's audit after a final input recheck."""

        if not isinstance(candidate, ComponentResolutionAudit):
            raise TypeError("resolution-audit update requires typed evidence")
        content = _audit_bytes(candidate)
        if len(content) > _MAX_PERSISTED_BYTES:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.limit_exceeded",
                "resolution audit exceeds the persisted byte limit",
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
                    prefix=f".{self.path.name}.", dir=self.storage_root
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
                        raise ComponentResolutionAuditStoreError(
                            "component_resolution_audit.concurrent_change",
                            "resolution audit changed while replacement was prepared",
                        )
                    if path_is_link_or_reparse(self.path):
                        raise ComponentResolutionAuditStoreError(
                            "component_resolution_audit.path_unsafe",
                            "resolution-audit destination cannot be a link or "
                            "reparse point",
                        )
                    if self.path.exists() and not self.path.is_file():
                        raise ComponentResolutionAuditStoreError(
                            "component_resolution_audit.path_unsafe",
                            "resolution-audit destination must be a regular file",
                        )
                    os.replace(temporary, self.path)
                    _fsync_directory(self.storage_root)
                finally:
                    temporary.unlink(missing_ok=True)
        except ComponentResolutionAuditStoreError:
            raise
        except (CacheLockError, OSError) as exc:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.write_failed",
                "resolution audit could not be committed atomically",
            ) from exc
        return True

    def _parse_canonical(self, content: bytes) -> ComponentResolutionAudit:
        try:
            audit = ComponentResolutionAudit.from_dict(json.loads(content))
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.invalid",
                "resolution audit is malformed or forged",
            ) from exc
        if content != _audit_bytes(audit):
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.noncanonical",
                "resolution audit does not use canonical bytes",
            )
        return audit

    def _read_bytes(self, *, required: bool) -> bytes | None:
        self._require_root_current()
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            if required:
                raise ComponentResolutionAuditStoreError(
                    "component_resolution_audit.missing",
                    "resolution audit does not exist",
                ) from None
            return None
        except OSError as exc:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.read_failed",
                "resolution audit could not be inspected",
            ) from exc
        if stat_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.path_unsafe",
                "resolution-audit destination must be a direct regular file",
            )
        if metadata.st_size > _MAX_PERSISTED_BYTES:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.limit_exceeded",
                "resolution audit exceeds the persisted byte limit",
            )
        try:
            with self.path.open("rb") as stream:
                content = stream.read(_MAX_PERSISTED_BYTES + 1)
            after = self.path.lstat()
        except OSError as exc:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.read_failed",
                "resolution audit could not be read",
            ) from exc
        if (
            stat_is_link_or_reparse(after)
            or not stat.S_ISREG(after.st_mode)
            or _node_signature(metadata) != _node_signature(after)
        ):
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.concurrent_change",
                "resolution audit changed while it was read",
            )
        if len(content) > _MAX_PERSISTED_BYTES:
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.limit_exceeded",
                "resolution audit exceeds the persisted byte limit",
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
                raise ComponentResolutionAuditStoreError(
                    "component_resolution_audit.path_unsafe",
                    "resolution-audit destination cannot be a link or reparse point",
                )
            if self.path.exists() and not self.path.is_file():
                raise ComponentResolutionAuditStoreError(
                    "component_resolution_audit.path_unsafe",
                    "resolution-audit destination must be a regular file",
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
            raise ComponentResolutionAuditStoreError(
                "component_resolution_audit.root_unsafe",
                "Component root became unsafe",
            ) from exc


def _audit_bytes(audit: ComponentResolutionAudit) -> bytes:
    return canonical_json_bytes(audit.to_dict()) + b"\n"


def _node_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ComponentResolutionAuditCheck",
    "ComponentResolutionAuditStore",
    "ComponentResolutionAuditStoreError",
]
