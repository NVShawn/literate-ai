"""Mutable indexes over immutable CAS objects."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)

from .cas import BlobRef, FileSystemCAS, StorageError, StorageSafetyError
from .events import FileLock

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_MAXIMUM_REFERENCE_SNAPSHOT_BYTES = 16 * 1024 * 1024
_DEPENDENCY_KINDS = frozenset(
    {
        "direct",
        "transitive",
        "build",
        "runtime",
        "optional",
        "capability",
        "provider-generated",
    }
)


class IndexError(StorageError):
    """A reference or dependency index operation failed."""


class ReferenceConflictError(IndexError):
    """A compare-and-swap precondition did not match current state."""


@dataclass(frozen=True, slots=True)
class ReferenceRecord:
    namespace: str
    name: str
    target: BlobRef
    generation: int
    updated_at: str

    @property
    def key(self) -> str:
        return f"{self.namespace}:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "target": self.target.to_dict(),
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReferenceRecord:
        return cls(
            namespace=str(value["namespace"]),
            name=str(value["name"]),
            target=BlobRef.from_dict(value["target"]),
            generation=int(value["generation"]),
            updated_at=str(value["updated_at"]),
        )


class ReferenceIndex:
    """CAS-backed aliases; read-only snapshots never create roots or take locks.

    Writers publish atomically. A racing publication may refuse a read-only
    observation, which callers can retry; it must not become a false cache miss.
    """

    def __init__(
        self, root: str | Path, cas: FileSystemCAS, *, read_only: bool = False
    ) -> None:
        self.cas = cas
        self.read_only = read_only
        if read_only:
            self.root = Path(root).expanduser().absolute()
            _require_reference_directory(self.root)
        else:
            self.root = _safe_root(root)
        self.path = self.root / "references.json"
        self.lock_path = self.root / ".references.lock"

    def set(
        self,
        namespace: str,
        name: str,
        target: BlobRef,
        *,
        expected_generation: int | None = None,
    ) -> ReferenceRecord:
        if self.read_only:
            raise StorageSafetyError("read-only reference index cannot be changed")
        _validate_name(namespace)
        _validate_name(name)
        self.cas.verify(target)
        key = _reference_key(namespace, name)
        with FileLock(self.lock_path):
            document = self._load()
            previous_raw = document["references"].get(key)
            previous = ReferenceRecord.from_dict(previous_raw) if previous_raw else None
            current_generation = previous.generation if previous else 0
            if (
                expected_generation is not None
                and expected_generation != current_generation
            ):
                raise ReferenceConflictError(f"reference {key!r} generation changed")
            record = ReferenceRecord(
                namespace=namespace,
                name=name,
                target=target,
                generation=current_generation + 1,
                updated_at=datetime.now(UTC).isoformat(),
            )
            document["references"][key] = record.to_dict()
            _atomic_json_write(self.path, document, self.root)
            return record

    def resolve(self, namespace: str, name: str) -> ReferenceRecord | None:
        _validate_name(namespace)
        _validate_name(name)
        with nullcontext() if self.read_only else FileLock(self.lock_path):
            raw = self._load()["references"].get(_reference_key(namespace, name))
        if raw is None:
            return None
        record = ReferenceRecord.from_dict(raw)
        self.cas.verify(record.target)
        return record

    def list(self, namespace: str | None = None) -> tuple[ReferenceRecord, ...]:
        if namespace is not None:
            _validate_name(namespace)
        with nullcontext() if self.read_only else FileLock(self.lock_path):
            values = self._load()["references"].values()
            records = [ReferenceRecord.from_dict(item) for item in values]
        return tuple(
            sorted(
                (
                    item
                    for item in records
                    if namespace is None or item.namespace == namespace
                ),
                key=lambda item: item.key,
            )
        )

    def _load(self) -> dict[str, Any]:
        if self.read_only:
            return _read_reference_snapshot(self.path)
        return _load_document(self.path, "references")


def _require_reference_directory(path: Path) -> None:
    try:
        require_safe_directory(path)
    except (OSError, UnsafeFilesystemPathError) as exc:
        raise StorageSafetyError(
            "reference index directory is unavailable or unsafe"
        ) from exc


def _read_reference_snapshot(path: Path) -> dict[str, Any]:
    """Read one bounded atomic publication without creating a lock or directory."""
    _require_reference_directory(path.parent)
    try:
        before = path.lstat()
    except FileNotFoundError:
        _require_reference_directory(path.parent)
        return {"schema_version": 1, "references": {}}
    except OSError as exc:
        raise StorageSafetyError("reference index is unavailable") from exc
    if stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise StorageSafetyError("reference index must be a direct regular file")
    if before.st_size > _MAXIMUM_REFERENCE_SNAPSHOT_BYTES:
        raise IndexError("reference index exceeds the snapshot byte limit")
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY", "O_CLOEXEC"):
        flags |= getattr(os, name, 0)

    def signature(node: os.stat_result) -> tuple[int, ...]:
        return (
            node.st_dev,
            node.st_ino,
            node.st_size,
            node.st_mtime_ns,
            node.st_ctime_ns,
            node.st_mode,
        )

    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            named_signature, opened_signature = signature(before), signature(opened)
            _require_reference_directory(path.parent)
            # Pathname ctime and descriptor ctime need not share Windows semantics.
            # Preserve both full observations while cross-binding the shared fields.
            if (
                opened_signature[:4] != named_signature[:4]
                or opened_signature[-1] != named_signature[-1]
                or signature(path.lstat()) != named_signature
            ):
                raise IndexError("reference index changed before reading")
            chunks = []
            remaining = before.st_size + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            _require_reference_directory(path.parent)
            content = b"".join(chunks)
            if (
                len(content) != before.st_size
                or signature(os.fstat(descriptor)) != opened_signature
                or signature(path.lstat()) != named_signature
            ):
                raise IndexError("reference index changed while reading")
        finally:
            os.close(descriptor)
        value = json.loads(content)
    except (OSError, ValueError, UnicodeError) as exc:
        raise IndexError("reference index snapshot is unreadable") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("references"), dict)
    ):
        raise IndexError("reference index has an unsupported schema")
    return value


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    subject_id: str
    dependency_id: str
    kind: str
    required: bool = True
    artifact: BlobRef | None = None

    def __post_init__(self) -> None:
        _validate_name(self.subject_id)
        _validate_name(self.dependency_id)
        if self.subject_id == self.dependency_id:
            raise ValueError("a dependency edge cannot reference itself")
        if self.kind not in _DEPENDENCY_KINDS:
            raise ValueError(f"unsupported dependency kind {self.kind!r}")
        if self.kind == "optional" and self.required:
            raise ValueError("optional dependency edges cannot be required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "dependency_id": self.dependency_id,
            "kind": self.kind,
            "required": self.required,
            "artifact": self.artifact.to_dict() if self.artifact else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DependencyEdge:
        artifact = value.get("artifact")
        return cls(
            subject_id=str(value["subject_id"]),
            dependency_id=str(value["dependency_id"]),
            kind=str(value["kind"]),
            required=bool(value.get("required", True)),
            artifact=BlobRef.from_dict(artifact) if artifact else None,
        )


class DependencyIndex:
    """Forward dependencies and a derived reverse-dependency projection."""

    def __init__(
        self,
        root: str | Path,
        *,
        cas: FileSystemCAS | None = None,
    ) -> None:
        self.cas = cas
        self.root = _safe_root(root)
        self.path = self.root / "dependencies.json"
        self.lock_path = self.root / ".dependencies.lock"

    def replace(self, subject_id: str, edges: Iterable[DependencyEdge]) -> None:
        _validate_name(subject_id)
        normalized = tuple(edges)
        keys: set[tuple[str, str]] = set()
        for edge in normalized:
            if edge.subject_id != subject_id:
                raise ValueError("all dependency edges must match the replaced subject")
            key = (edge.dependency_id, edge.kind)
            if key in keys:
                raise ValueError("duplicate dependency edge")
            keys.add(key)
            if edge.required and edge.artifact is None:
                raise IndexError(
                    f"required dependency {edge.dependency_id!r} has no artifact"
                )
            if edge.artifact is not None and self.cas is not None:
                self.cas.verify(edge.artifact)
        with FileLock(self.lock_path):
            document = self._load()
            document["subjects"][subject_id] = [
                edge.to_dict()
                for edge in sorted(
                    normalized, key=lambda item: (item.kind, item.dependency_id)
                )
            ]
            _atomic_json_write(self.path, document, self.root)

    def dependencies(self, subject_id: str) -> tuple[DependencyEdge, ...]:
        _validate_name(subject_id)
        with FileLock(self.lock_path):
            raw = self._load()["subjects"].get(subject_id, [])
        return tuple(DependencyEdge.from_dict(item) for item in raw)

    def dependents(self, dependency_id: str) -> tuple[DependencyEdge, ...]:
        _validate_name(dependency_id)
        with FileLock(self.lock_path):
            document = self._load()
        result = [
            DependencyEdge.from_dict(raw)
            for edges in document["subjects"].values()
            for raw in edges
            if raw.get("dependency_id") == dependency_id
        ]
        return tuple(sorted(result, key=lambda item: (item.subject_id, item.kind)))

    def closure(
        self,
        subject_id: str,
        *,
        include_optional: bool = False,
    ) -> tuple[str, ...]:
        _validate_name(subject_id)
        with FileLock(self.lock_path):
            document = self._load()
        ordered: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(current: str) -> None:
            if current in visited:
                return
            if current in visiting:
                raise IndexError(f"dependency cycle includes {current!r}")
            visiting.add(current)
            raw_edges = document["subjects"].get(current, [])
            for raw in raw_edges:
                edge = DependencyEdge.from_dict(raw)
                if not edge.required and not include_optional:
                    continue
                visit(edge.dependency_id)
            visiting.remove(current)
            visited.add(current)
            if current != subject_id:
                ordered.append(current)

        visit(subject_id)
        return tuple(ordered)

    def _load(self) -> dict[str, Any]:
        return _load_document(self.path, "subjects")


def _safe_root(root: str | Path) -> Path:
    configured = Path(root).expanduser()
    if configured.is_symlink():
        raise StorageSafetyError("index root must not be a symbolic link")
    configured.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = configured.resolve(strict=True)
    if not resolved.is_dir():
        raise StorageSafetyError("index root must be a directory")
    return resolved


def _validate_name(value: str) -> None:
    if not _SAFE_NAME.fullmatch(value) or ".." in value.split("/"):
        raise ValueError("index name contains unsafe characters")


def _reference_key(namespace: str, name: str) -> str:
    """Encode an unambiguous composite key even when IDs contain separators."""

    return json.dumps([namespace, name], separators=(",", ":"), ensure_ascii=True)


def _load_document(path: Path, collection: str) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, collection: {}}
    if path.is_symlink() or not path.is_file():
        raise StorageSafetyError("index document must be a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IndexError("index document is unreadable") from exc
    if document.get("schema_version") != 1 or not isinstance(
        document.get(collection), dict
    ):
        raise IndexError("index document has an unsupported schema")
    return document


def _atomic_json_write(path: Path, value: Mapping[str, Any], root: Path) -> None:
    if path.is_symlink():
        raise StorageSafetyError("index document must not be a symbolic link")
    if path.parent.resolve(strict=True) != root:
        raise StorageSafetyError("index document escaped its managed root")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=root, text=True)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp_path, 0o600)
        temp_path.replace(path)
        if os.name != "nt":
            # Windows cannot open directory descriptors through the CRT.
            descriptor = os.open(root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temp_path.unlink(missing_ok=True)
