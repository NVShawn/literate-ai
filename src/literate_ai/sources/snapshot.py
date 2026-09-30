"""Canonical local-tree and aggregate-source snapshot construction."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.contracts import (
    AggregateSourceMember,
    ContentIdentity,
    HashAlgorithm,
    SourceEntry,
    SourceEntryType,
    SourceSnapshot,
    canonical_identity,
)

from .models import GitFacts, GitSubmoduleFact, LfsPointerFact, SourceCapture

_BUFFER_SIZE = 1024 * 1024


class SourceSnapshotError(RuntimeError):
    """Source bytes cannot be captured without ambiguity or path escape."""


@dataclass(frozen=True, slots=True)
class SnapshotPolicy:
    """Portable file-selection policy included in provider configuration."""

    excluded_top_level: tuple[str, ...] = (".codegraph", ".git")
    reject_symlinks: bool = True

    def __post_init__(self) -> None:
        if not self.reject_symlinks:
            raise ValueError("schema v1 snapshots require symlink rejection")
        for name in self.excluded_top_level:
            if not name or "/" in name or name in {".", ".."}:
                raise ValueError("excluded top-level names must be simple path names")
        if len(set(self.excluded_top_level)) != len(self.excluded_top_level):
            raise ValueError("excluded top-level names must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "urn:literate-ai:schema:v1:snapshot-policy",
            "excluded_top_level": sorted(self.excluded_top_level),
            "reject_symlinks": self.reject_symlinks,
        }


class SourceSnapshotter:
    """Hash source bytes and modes into canonical portable contracts."""

    def __init__(self, policy: SnapshotPolicy | None = None) -> None:
        self.policy = policy or SnapshotPolicy()

    def snapshot_local(self, root: str | Path) -> SourceCapture:
        source_root = self._root(root)
        paths = self._walk_paths(source_root)
        entries, pointers = self._entries(source_root, paths)
        snapshot = SourceSnapshot.create(
            source_provider="local",
            provider_configuration=canonical_identity(self.policy.to_dict()),
            resolved_revision="working-tree",
            entries=entries,
        )
        return SourceCapture(snapshot=snapshot, lfs_pointers=pointers)

    def snapshot_git_paths(
        self,
        root: str | Path,
        paths: Iterable[str],
        git: GitFacts,
    ) -> SourceCapture:
        """Snapshot exactly Git-tracked paths plus explicit gitlink entries."""

        source_root = self._root(root)
        submodule_paths = {item.path for item in git.submodules}
        normalized = tuple(
            sorted(
                self._normalize_relative(path)
                for path in paths
                if self._normalize_relative(path) not in submodule_paths
            )
        )
        entries, pointers = self._entries(source_root, normalized)
        submodule_entries = tuple(
            self._submodule_entry(item)
            for item in sorted(git.submodules, key=lambda fact: fact.path)
        )
        all_entries = tuple(
            sorted(entries + submodule_entries, key=lambda item: item.path)
        )
        dirty_policy = (
            canonical_identity(
                {
                    "schema": "urn:literate-ai:schema:v1:dirty-source-decision",
                    "decision": "quarantine-only",
                }
            )
            if git.dirty
            else None
        )
        snapshot = SourceSnapshot.create(
            source_provider="git",
            provider_configuration=canonical_identity(
                {
                    "schema": "urn:literate-ai:schema:v1:git-source-provider",
                    "snapshot_policy": self.policy.to_dict(),
                    "tracked_files_only": True,
                }
            ),
            resolved_revision=git.head_commit,
            entries=all_entries,
            dirty=git.dirty,
            dirty_policy_decision=dirty_policy,
        )
        return SourceCapture(snapshot=snapshot, lfs_pointers=pointers, git=git)

    @staticmethod
    def aggregate(
        members: Iterable[tuple[str, str, SourceCapture]],
    ) -> SourceCapture:
        """Create a deterministic higher-level source from exact child snapshots."""

        aggregate_members = tuple(
            sorted(
                (
                    AggregateSourceMember(
                        member_id=member_id,
                        mount_path=mount_path,
                        snapshot_identity=capture.snapshot.identity,
                    )
                    for member_id, mount_path, capture in members
                ),
                key=lambda item: (item.mount_path, item.member_id),
            )
        )
        if not aggregate_members:
            raise ValueError("aggregate sources require at least one member")
        if len({item.member_id for item in aggregate_members}) != len(
            aggregate_members
        ):
            raise ValueError("aggregate member IDs must be unique")
        if len({item.mount_path for item in aggregate_members}) != len(
            aggregate_members
        ):
            raise ValueError("aggregate mount paths must be unique")
        revision = canonical_identity(
            [item.to_dict() for item in aggregate_members]
        ).uri
        snapshot = SourceSnapshot.create(
            source_provider="aggregate",
            provider_configuration=canonical_identity(
                {
                    "schema": "urn:literate-ai:schema:v1:aggregate-source-provider",
                    "layout": "mounted-members",
                }
            ),
            resolved_revision=revision,
            aggregate_members=aggregate_members,
        )
        return SourceCapture(snapshot=snapshot)

    def _walk_paths(self, root: Path) -> tuple[str, ...]:
        result: list[str] = []
        excluded = set(self.policy.excluded_top_level)
        for current, directories, files in os.walk(
            root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            relative_current = current_path.relative_to(root)
            kept_directories: list[str] = []
            for name in sorted(directories):
                path = current_path / name
                if path.is_symlink():
                    raise SourceSnapshotError(f"symbolic link is not allowed: {path}")
                if relative_current == Path(".") and name in excluded:
                    continue
                kept_directories.append(name)
            directories[:] = kept_directories
            for name in sorted(files):
                path = current_path / name
                if path.is_symlink():
                    raise SourceSnapshotError(f"symbolic link is not allowed: {path}")
                relative = path.relative_to(root).as_posix()
                if relative_current == Path(".") and name in excluded:
                    continue
                result.append(relative)
        return tuple(sorted(result))

    def _entries(
        self,
        root: Path,
        paths: Iterable[str],
    ) -> tuple[tuple[SourceEntry, ...], tuple[LfsPointerFact, ...]]:
        entries: list[SourceEntry] = []
        pointers: list[LfsPointerFact] = []
        seen: set[str] = set()
        for relative in paths:
            relative = self._normalize_relative(relative)
            if relative in seen:
                raise SourceSnapshotError(f"duplicate source path: {relative}")
            seen.add(relative)
            path = root / relative
            if path.is_symlink():
                raise SourceSnapshotError(f"symbolic link is not allowed: {path}")
            try:
                file_stat = path.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                raise SourceSnapshotError(
                    f"source path disappeared: {relative}"
                ) from exc
            if not stat.S_ISREG(file_stat.st_mode):
                raise SourceSnapshotError(
                    f"source path is not a regular file: {relative}"
                )
            digest, size, prefix = self._hash_file(path)
            mode = 0o755 if file_stat.st_mode & 0o111 else 0o644
            entries.append(
                SourceEntry(
                    path=relative,
                    entry_type=SourceEntryType.FILE,
                    mode=mode,
                    size=size,
                    identity=ContentIdentity(HashAlgorithm.SHA256, digest),
                )
            )
            pointer = self._lfs_pointer(relative, prefix, size)
            if pointer is not None:
                pointers.append(pointer)
        return (
            tuple(sorted(entries, key=lambda item: item.path)),
            tuple(sorted(pointers, key=lambda item: item.path)),
        )

    @staticmethod
    def _submodule_entry(fact: GitSubmoduleFact) -> SourceEntry:
        return SourceEntry(
            path=fact.path,
            entry_type=SourceEntryType.SUBMODULE,
            mode=0,
            size=0,
            identity=canonical_identity(
                {"gitlink_commit": fact.commit, "state": fact.state}
            ),
        )

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int, bytes]:
        digest = hashlib.sha256()
        size = 0
        prefix = bytearray()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise SourceSnapshotError(
                f"cannot safely open source file: {path}"
            ) from exc
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise SourceSnapshotError(f"source path is not a regular file: {path}")
            while chunk := source.read(_BUFFER_SIZE):
                digest.update(chunk)
                size += len(chunk)
                if len(prefix) < 1024:
                    prefix.extend(chunk[: 1024 - len(prefix)])
        return digest.hexdigest(), size, bytes(prefix)

    @staticmethod
    def _lfs_pointer(path: str, prefix: bytes, size: int) -> LfsPointerFact | None:
        if size > 1024:
            return None
        try:
            lines = prefix.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return None
        if not lines or lines[0] != "version https://git-lfs.github.com/spec/v1":
            return None
        values: dict[str, str] = {}
        for line in lines[1:]:
            key, separator, value = line.partition(" ")
            if separator:
                values[key] = value
        oid = values.get("oid", "")
        declared_size = values.get("size", "")
        if not oid.startswith("sha256:") or not declared_size.isdecimal():
            return None
        try:
            return LfsPointerFact(path, oid.removeprefix("sha256:"), int(declared_size))
        except ValueError:
            return None

    @staticmethod
    def _root(root: str | Path) -> Path:
        configured = Path(root).expanduser()
        if configured.is_symlink():
            raise SourceSnapshotError("source root must not be a symbolic link")
        try:
            resolved = configured.resolve(strict=True)
        except OSError as exc:
            raise SourceSnapshotError("source root must exist") from exc
        if not resolved.is_dir():
            raise SourceSnapshotError("source root must be a directory")
        return resolved

    @staticmethod
    def _normalize_relative(value: str) -> str:
        if not isinstance(value, str) or not value or "\\" in value:
            raise SourceSnapshotError("source paths must be relative POSIX paths")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise SourceSnapshotError(f"source path is not normalized: {value!r}")
        return path.as_posix()


__all__ = ["SnapshotPolicy", "SourceSnapshotError", "SourceSnapshotter"]
