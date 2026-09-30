"""Immutable raw Git trees and prospective deltas, not checkout authorization."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from .identity import canonical_identity
from .paths import canonical_relative_posix_path

_MODES = {"040000", "100644", "100755", "120000", "160000"}


def git_object_identity(kind: str, content: bytes, width: int) -> str:
    if kind not in {"commit", "tree", "blob"} or width not in {40, 64}:
        raise ValueError("unsupported Git object kind or format")
    if not isinstance(content, bytes):
        raise TypeError("Git object content must be bytes")
    payload = (
        kind.encode("ascii")
        + b" "
        + str(len(content)).encode("ascii")
        + b"\0"
        + content
    )
    return (hashlib.sha1 if width == 40 else hashlib.sha256)(payload).hexdigest()


def _oid(value: str) -> None:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)
        or not value.strip("0")
    ):
        raise ValueError("Git object identity must be exact and nonzero")


@dataclass(frozen=True, slots=True)
class RepositoryTreeCapturePolicy:
    maximum_entries: int = 8192
    maximum_metadata_bytes: int = 2 * 1024 * 1024
    maximum_blob_bytes: int = 16 * 1024 * 1024
    maximum_total_blob_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, upper in (
            ("maximum_entries", 8192),
            ("maximum_metadata_bytes", 2 * 1024 * 1024),
            ("maximum_blob_bytes", 16 * 1024 * 1024),
            ("maximum_total_blob_bytes", 64 * 1024 * 1024),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(
                    "tree capture bounds must be positive integers within hard limits"
                )


@dataclass(frozen=True, slots=True)
class RepositoryTreeEntry:
    path: str
    mode: str
    object_id: str
    content: bytes | None = None

    def __post_init__(self) -> None:
        path = canonical_relative_posix_path(self.path, label="repository tree entry")
        if len(self.path.encode("utf-8")) > 4096 or len(path.parts) > 128:
            raise ValueError("repository tree path exceeds its bound")
        if any(part.casefold() == ".git" for part in path.parts):
            raise ValueError("repository tree cannot address Git metadata")
        if self.mode not in _MODES:
            raise ValueError("unsupported Git tree entry mode")
        _oid(self.object_id)
        if self.mode in {"040000", "160000"}:
            if self.content is not None:
                raise ValueError("tree and Gitlink entries do not contain blob bytes")
        elif (
            not isinstance(self.content, bytes) or len(self.content) > 16 * 1024 * 1024
        ):
            raise ValueError("blob entry requires bounded bytes")
        elif (
            git_object_identity("blob", self.content, len(self.object_id))
            != self.object_id
        ):
            raise ValueError("blob bytes do not match the Git object identity")


@dataclass(frozen=True, slots=True)
class RepositoryTreeSnapshot:
    commit: str
    commit_content: bytes
    entries: tuple[RepositoryTreeEntry, ...]

    def __post_init__(self) -> None:
        _oid(self.commit)
        if (
            not isinstance(self.commit_content, bytes)
            or len(self.commit_content) > 2 * 1024 * 1024
        ):
            raise ValueError("commit content must be bounded bytes")
        if (
            git_object_identity("commit", self.commit_content, len(self.commit))
            != self.commit
        ):
            raise ValueError("commit bytes do not match the requested identity")
        header = self.commit_content.partition(b"\n")[0]
        if header != b"tree " + self.tree_id.encode("ascii"):
            raise ValueError("commit must begin with one exact tree identity")
        _oid(self.tree_id)
        if len(self.tree_id) != len(self.commit):
            raise ValueError("commit and tree formats must agree")
        if not isinstance(self.entries, tuple) or len(self.entries) > 8192:
            raise ValueError("tree entries must be a bounded immutable tuple")
        if any(not isinstance(entry, RepositoryTreeEntry) for entry in self.entries):
            raise TypeError("tree snapshot requires typed entries")
        if tuple(sorted(self.entries, key=lambda entry: entry.path)) != self.entries:
            raise ValueError("tree entries must use canonical path order")
        paths = {entry.path: entry for entry in self.entries}
        aliases = {
            unicodedata.normalize("NFC", entry.path.casefold())
            for entry in self.entries
        }
        if len(paths) != len(self.entries) or len(aliases) != len(paths):
            raise ValueError("tree paths duplicate or alias")
        if sum(len(entry.content or b"") for entry in self.entries) > 64 * 1024 * 1024:
            raise ValueError("tree blob bytes exceed the aggregate bound")
        children = defaultdict(list)
        for entry in self.entries:
            if len(entry.object_id) != len(self.commit):
                raise ValueError("tree object formats must agree")
            parent = PurePosixPath(entry.path).parent.as_posix()
            if parent != "." and (
                parent not in paths or paths[parent].mode != "040000"
            ):
                raise ValueError("every entry requires its exact directory parent")
            children[parent].append(entry)
        directories = {
            ".": self.tree_id,
            **{
                entry.path: entry.object_id
                for entry in self.entries
                if entry.mode == "040000"
            },
        }
        for path, expected in directories.items():
            members = sorted(
                children[path],
                key=lambda entry: (
                    PurePosixPath(entry.path).name
                    + ("/" if entry.mode == "040000" else "")
                ).encode("utf-8"),
            )
            content = b"".join(
                entry.mode.lstrip("0").encode("ascii")
                + b" "
                + PurePosixPath(entry.path).name.encode("utf-8")
                + b"\0"
                + bytes.fromhex(entry.object_id)
                for entry in members
            )
            if git_object_identity("tree", content, len(self.commit)) != expected:
                raise ValueError(
                    "tree entries are incomplete or disagree with Git tree identity"
                )

    @property
    def tree_id(self) -> str:
        try:
            return (
                self.commit_content.partition(b"\n")[0]
                .removeprefix(b"tree ")
                .decode("ascii")
            )
        except UnicodeError as error:
            raise ValueError("commit tree identity is invalid") from error

    @property
    def identity(self) -> str:
        return canonical_identity(
            {
                "commit": self.commit,
                "commit_bytes": hashlib.sha256(self.commit_content).hexdigest(),
                "entries": [
                    {
                        "path": entry.path,
                        "mode": entry.mode,
                        "object": entry.object_id,
                        "content": None
                        if entry.content is None
                        else hashlib.sha256(entry.content).hexdigest(),
                    }
                    for entry in self.entries
                ],
            }
        ).uri

    @property
    def metadata_size_bound(self) -> int:
        """Conservative raw commit/listing bytes, not Python heap accounting."""
        return len(self.commit_content) + sum(
            len(entry.path.encode("utf-8")) + len(entry.object_id) + 32
            for entry in self.entries
        )


@dataclass(frozen=True, slots=True)
class RepositoryTreeChange:
    path: str
    previous: RepositoryTreeEntry | None
    prospective: RepositoryTreeEntry | None

    def __post_init__(self) -> None:
        canonical_relative_posix_path(self.path, label="repository tree change")
        for entry in (self.previous, self.prospective):
            if entry is not None and (
                not isinstance(entry, RepositoryTreeEntry) or entry.path != self.path
            ):
                raise ValueError("tree change members must match the exact path")
        if self.previous == self.prospective:
            raise ValueError("tree change requires distinct before/after entries")


def repository_tree_changes(
    previous: RepositoryTreeSnapshot, prospective: RepositoryTreeSnapshot
) -> tuple[RepositoryTreeChange, ...]:
    """Exact path changes, not an ordered filesystem execution/rollback plan."""
    if not isinstance(previous, RepositoryTreeSnapshot) or not isinstance(
        prospective, RepositoryTreeSnapshot
    ):
        raise TypeError("tree differences require complete verified snapshots")
    if len(previous.commit) != len(prospective.commit):
        raise ValueError("refresh cannot change a repository's Git object format")
    before = {entry.path: entry for entry in previous.entries}
    after = {entry.path: entry for entry in prospective.entries}
    return tuple(
        RepositoryTreeChange(path, before.get(path), after.get(path))
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path)
        and not (
            path in before
            and path in after
            and before[path].mode == after[path].mode == "040000"
        )
    )
