"""Bounded shards of canonical tree entries; composition grants no admission.

These descriptors reuse NativeSdkFile/NativeSdkAlias. A shard is deliberately not
a standalone NativeSdkTree: its ancestors and alias targets may live in another
shard, and only complete composition can validate that closure. Frozen standalone
tree limits and validation remain unchanged.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, ClassVar

from ._validation import contract_fields
from .blobs import BlobRef
from .identity import canonical_json_bytes, contract_identity
from .native_sdk_trees import NativeSdkAlias, NativeSdkTree, _ResolutionBudget
from .native_sdks import NativeSdkFile
from .paths import canonical_relative_posix_path
from .retained_project import RetainedProjectLimits

MAX_TREE_MEMBERS = 1_048_576
MAX_SHARD_MEMBERS = 16_384
MAX_TREE_SHARDS = 4096
MAX_DESCRIPTOR_BYTES = 8 * 1024 * 1024
MAX_DESCRIPTOR_TOTAL_BYTES = 512 * 1024 * 1024
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_METADATA_MEMORY_BYTES = 16 * 1024 * 1024 * 1024


def _integer(value: Any, minimum: int, maximum: int, label: str) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"tree.shards.{label}-out-of-bounds")


def _key(path: str, case_sensitive: bool) -> str:
    return path if case_sensitive else unicodedata.normalize("NFC", path.casefold())


def validate_native_tree_policy(
    limits,
    case_sensitive,
    max_descriptor_bytes,
    max_descriptor_total_bytes,
    max_index_bytes,
    max_metadata_memory_bytes,
):
    if (
        not isinstance(limits, RetainedProjectLimits)
        or type(case_sensitive) is not bool
    ):
        raise TypeError("tree.shards.typed-policy-required")
    for name, value, maximum in (
        ("max_descriptor_bytes", max_descriptor_bytes, MAX_DESCRIPTOR_BYTES),
        (
            "max_descriptor_total_bytes",
            max_descriptor_total_bytes,
            MAX_DESCRIPTOR_TOTAL_BYTES,
        ),
        ("max_index_bytes", max_index_bytes, MAX_INDEX_BYTES),
        (
            "max_metadata_memory_bytes",
            max_metadata_memory_bytes,
            MAX_METADATA_MEMORY_BYTES,
        ),
    ):
        _integer(value, 1, maximum, name)


@dataclass(frozen=True, slots=True)
class NativeTreeShard:
    directories: tuple[str, ...]
    files: tuple[NativeSdkFile, ...]
    aliases: tuple[NativeSdkAlias, ...]
    case_sensitive: bool = False

    SCHEMA: ClassVar[str] = "literate-ai/native-tree-shard@1"

    def __post_init__(self):
        if type(self.case_sensitive) is not bool or any(
            type(group) is not tuple
            for group in (self.directories, self.files, self.aliases)
        ):
            raise TypeError("tree.shards.typed-canonical-tuples-required")
        _integer(self.member_count, 1, MAX_SHARD_MEMBERS, "member-count")
        if any(not isinstance(v, NativeSdkFile) for v in self.files) or any(
            not isinstance(v, NativeSdkAlias) for v in self.aliases
        ):
            raise TypeError("tree.shards.canonical-entry-types-required")
        names = set()
        for group in (
            self.directories,
            tuple(v.path for v in self.files),
            tuple(v.path for v in self.aliases),
        ):
            if group != tuple(sorted(group)):
                raise ValueError("tree.shards.canonical-order-required")
            for name in group:
                canonical_relative_posix_path(name, label="tree shard member")
                key = _key(name, self.case_sensitive)
                if key in names:
                    raise ValueError("tree.shards.ambiguous-member")
                names.add(key)
        if len(canonical_json_bytes(self.to_dict())) > MAX_DESCRIPTOR_BYTES:
            raise ValueError("tree.shards.descriptor-byte-bound-exceeded")

    @property
    def member_count(self):
        return len(self.directories) + len(self.files) + len(self.aliases)

    @property
    def payload_bytes(self):
        return sum(v.blob.size for v in self.files) + sum(
            len(v.target.encode("utf-8")) for v in self.aliases if v.kind == "symlink"
        )

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "directories": list(self.directories),
            "files": [v.to_dict() for v in self.files],
            "aliases": [v.to_dict() for v in self.aliases],
            "case_sensitive": self.case_sensitive,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"directories", "files", "aliases", "case_sensitive"}),
        )
        groups = tuple(data[k] for k in ("directories", "files", "aliases"))
        if any(type(v) is not list for v in groups) or not (
            1 <= sum(map(len, groups)) <= MAX_SHARD_MEMBERS
        ):
            raise ValueError("tree.shards.descriptor-entry-bound-exceeded")
        return cls(
            tuple(data["directories"]),
            tuple(NativeSdkFile.from_dict(v) for v in data["files"]),
            tuple(NativeSdkAlias.from_dict(v) for v in data["aliases"]),
            data["case_sensitive"],
        )


@dataclass(frozen=True, slots=True)
class NativeTreeIndex:
    shards: tuple[BlobRef, ...]
    member_count: int
    payload_bytes: int
    limits: RetainedProjectLimits
    case_sensitive: bool = False
    max_descriptor_bytes: int = MAX_DESCRIPTOR_BYTES
    max_descriptor_total_bytes: int = MAX_DESCRIPTOR_TOTAL_BYTES
    max_index_bytes: int = MAX_INDEX_BYTES
    max_metadata_memory_bytes: int = 1024 * 1024 * 1024

    SCHEMA: ClassVar[str] = "literate-ai/native-tree-index@1"

    def __post_init__(self):
        validate_native_tree_policy(
            self.limits,
            self.case_sensitive,
            self.max_descriptor_bytes,
            self.max_descriptor_total_bytes,
            self.max_index_bytes,
            self.max_metadata_memory_bytes,
        )
        if (
            type(self.shards) is not tuple
            or not 1 <= len(self.shards) <= MAX_TREE_SHARDS
            or any(not isinstance(v, BlobRef) for v in self.shards)
        ):
            raise ValueError("tree.shards.bounded-reference-index-required")
        identities = tuple(v.digest for v in self.shards)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("tree.shards.unique-canonical-references-required")
        _integer(
            self.member_count,
            1,
            min(self.limits.max_entries, MAX_TREE_MEMBERS),
            "count",
        )
        _integer(self.payload_bytes, 0, self.limits.max_total_bytes, "payload-bytes")
        if (
            any(
                not 0 < v.size <= self.max_descriptor_bytes
                or v.media_type != "application/json"
                for v in self.shards
            )
            or sum(v.size for v in self.shards) > self.max_descriptor_total_bytes
        ):
            raise ValueError("tree.shards.descriptor-byte-bound-exceeded")
        if len(canonical_json_bytes(self.to_dict())) > self.max_index_bytes:
            raise ValueError("tree.shards.index-byte-bound-exceeded")
        if self.metadata_memory_bound > self.max_metadata_memory_bytes:
            raise ValueError("tree.shards.metadata-memory-policy-exceeded")

    @property
    def metadata_memory_bound(self):
        # Conservative admission estimate, not a measured process/RSS claim.
        # Account for decoded objects, namespace indexes and one descriptor's
        # transient decoding/canonicalization. Native qualification measures peak.
        return (
            16
            * (
                sum(v.size for v in self.shards)
                + self.max_descriptor_bytes
                + self.max_index_bytes
            )
            + 2048 * self.member_count
            + 4096 * len(self.shards)
        )

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "shards": [v.to_dict() for v in self.shards],
            "member_count": self.member_count,
            "payload_bytes": self.payload_bytes,
            "limits": self.limits.to_dict(),
            "case_sensitive": self.case_sensitive,
            "max_descriptor_bytes": self.max_descriptor_bytes,
            "max_descriptor_total_bytes": self.max_descriptor_total_bytes,
            "max_index_bytes": self.max_index_bytes,
            "max_metadata_memory_bytes": self.max_metadata_memory_bytes,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "shards",
                    "member_count",
                    "payload_bytes",
                    "limits",
                    "case_sensitive",
                    "max_descriptor_bytes",
                    "max_descriptor_total_bytes",
                    "max_index_bytes",
                    "max_metadata_memory_bytes",
                }
            ),
        )
        if (
            type(data["shards"]) is not list
            or not 1 <= len(data["shards"]) <= MAX_TREE_SHARDS
        ):
            raise ValueError("tree.shards.bounded-reference-index-required")
        return cls(
            tuple(BlobRef.from_dict(v) for v in data["shards"]),
            data["member_count"],
            data["payload_bytes"],
            RetainedProjectLimits.from_dict(data["limits"]),
            data["case_sensitive"],
            data["max_descriptor_bytes"],
            data["max_descriptor_total_bytes"],
            data["max_index_bytes"],
            data["max_metadata_memory_bytes"],
        )


@dataclass(frozen=True, slots=True)
class ComposedNativeTree:
    """Complete bounded geometry, not a producer or PACKAGE/LINK capability."""

    index: NativeTreeIndex
    shards: tuple[NativeTreeShard, ...]
    directories: tuple[str, ...] = field(init=False, repr=False)
    files: tuple[NativeSdkFile, ...] = field(init=False, repr=False)
    aliases: tuple[NativeSdkAlias, ...] = field(init=False, repr=False)

    def __post_init__(self):
        if (
            not isinstance(self.index, NativeTreeIndex)
            or type(self.shards) is not tuple
        ):
            raise TypeError("tree.shards.typed-composition-required")
        if len(self.shards) != len(self.index.shards):
            raise ValueError("tree.shards.index-membership-mismatch")
        directories, files, aliases, names = set(), {}, {}, {}
        count = total = 0
        for shard, reference in zip(self.shards, self.index.shards, strict=True):
            if (
                not isinstance(shard, NativeTreeShard)
                or shard.case_sensitive != self.index.case_sensitive
            ):
                raise ValueError("tree.shards.case-policy-mismatch")
            content = canonical_json_bytes(shard.to_dict())
            if (
                shard.identity.digest != reference.digest
                or len(content) != reference.size
            ):
                raise ValueError("tree.shards.reference-mismatch")
            count += shard.member_count
            total += shard.payload_bytes
            if count > self.index.member_count or total > self.index.payload_bytes:
                raise ValueError("tree.shards.declared-totals-exceeded")
            for path in (
                *shard.directories,
                *(v.path for v in shard.files),
                *(v.path for v in shard.aliases),
            ):
                key = _key(path, self.index.case_sensitive)
                if key in names:
                    raise ValueError("tree.shards.ambiguous-composed-member")
                names[key] = path
            directories.update(shard.directories)
            files.update((v.path, v) for v in shard.files)
            aliases.update((v.path, v) for v in shard.aliases)
            if any(v.blob.size > self.index.limits.max_file_bytes for v in shard.files):
                raise ValueError("tree.shards.file-byte-bound-exceeded")
            if any(
                len(v.target.encode("utf-8")) > self.index.limits.max_file_bytes
                for v in shard.aliases
                if v.kind == "symlink"
            ):
                raise ValueError("tree.shards.alias-byte-bound-exceeded")
        if (count, total) != (self.index.member_count, self.index.payload_bytes):
            raise ValueError("tree.shards.declared-totals-mismatch")
        for path in names.values():
            for parent in PurePosixPath(path).parents:
                if (
                    parent != PurePosixPath(".")
                    and parent.as_posix() not in directories
                ):
                    raise ValueError("tree.shards.missing-directory-ancestor")
        budget = _ResolutionBudget()
        file_paths = set(files)
        for alias in aliases.values():
            NativeSdkTree._resolve(alias.path, directories, file_paths, aliases, budget)
        object.__setattr__(self, "directories", tuple(sorted(directories)))
        object.__setattr__(self, "files", tuple(files[k] for k in sorted(files)))
        object.__setattr__(self, "aliases", tuple(aliases[k] for k in sorted(aliases)))

    @property
    def identity(self):
        return self.index.identity

    @property
    def case_sensitive(self):
        return self.index.case_sensitive

    def resolve_all(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        """Batch logical observations share one index and resolution work budget."""
        if type(paths) is not tuple or len(paths) > MAX_TREE_MEMBERS:
            raise ValueError("tree.shards.bounded-logical-paths-required")
        directories = set(self.directories)
        files = {v.path for v in self.files}
        aliases = {v.path: v for v in self.aliases}
        budget = _ResolutionBudget()
        result = []
        for path in paths:
            canonical_relative_posix_path(path, label="composed native logical path")
            result.append(
                NativeSdkTree._resolve(path, directories, files, aliases, budget)
            )
        return tuple(result)
