"""Bounded selected-set result descriptors; decoding grants no live authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields
from .blobs import BlobRef
from .identity import ContentIdentity, canonical_json_bytes, contract_identity
from .native_tree_shards import (
    MAX_DESCRIPTOR_BYTES,
    MAX_DESCRIPTOR_TOTAL_BYTES,
    MAX_INDEX_BYTES,
    MAX_METADATA_MEMORY_BYTES,
    MAX_SHARD_MEMBERS,
    MAX_TREE_SHARDS,
    _integer,
)
from .retained_unit_packages import RetainedUnitPackageResult


@dataclass(frozen=True, slots=True)
class RetainedUnitPackageResultShard:
    results: tuple[RetainedUnitPackageResult, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-result-shard@1"

    def __post_init__(self):
        if (
            type(self.results) is not tuple
            or not 1 <= len(self.results) <= MAX_SHARD_MEMBERS
            or any(
                not isinstance(value, RetainedUnitPackageResult)
                for value in self.results
            )
        ):
            raise ValueError("retained.unit.bounded-result-shard-required")
        identities = tuple(value.definition_identity.uri for value in self.results)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("retained.unit.canonical-result-shard-required")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "results": [value.to_dict() for value in self.results],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"results"}),
        )
        rows = data["results"]
        if type(rows) is not list or not 1 <= len(rows) <= MAX_SHARD_MEMBERS:
            raise ValueError("retained.unit.bounded-result-shard-required")
        return cls(tuple(RetainedUnitPackageResult.from_dict(row) for row in rows))


@dataclass(frozen=True, slots=True)
class RetainedUnitPackageSetResult:
    package_set_plan_identity: ContentIdentity
    graph_identity: ContentIdentity
    packager_identity: ContentIdentity
    tool_identity: ContentIdentity
    shards: tuple[BlobRef, ...]
    unit_count: int
    archive_bytes: int
    max_descriptor_bytes: int
    max_descriptor_total_bytes: int
    max_index_bytes: int
    max_metadata_memory_bytes: int

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-set-result@1"
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "package_set_plan_identity",
        "graph_identity",
        "packager_identity",
        "tool_identity",
    )
    POLICY_FIELDS: ClassVar[tuple[str, ...]] = (
        "max_descriptor_bytes",
        "max_descriptor_total_bytes",
        "max_index_bytes",
        "max_metadata_memory_bytes",
    )

    def __post_init__(self):
        if any(
            not isinstance(getattr(self, key), ContentIdentity)
            for key in self.IDENTITY_FIELDS
        ):
            raise TypeError("retained.unit.typed-set-result-context-required")
        for key, maximum in zip(
            self.POLICY_FIELDS,
            (
                MAX_DESCRIPTOR_BYTES,
                MAX_DESCRIPTOR_TOTAL_BYTES,
                MAX_INDEX_BYTES,
                MAX_METADATA_MEMORY_BYTES,
            ),
            strict=True,
        ):
            _integer(getattr(self, key), 1, maximum, "unit-result-" + key)
        _integer(self.unit_count, 1, 1_000_000, "unit-result-count")
        _integer(self.archive_bytes, 1, 2**63 - 1, "unit-result-archive-bytes")
        if (
            type(self.shards) is not tuple
            or not 1 <= len(self.shards) <= min(MAX_TREE_SHARDS, self.unit_count)
            or self.unit_count > len(self.shards) * MAX_SHARD_MEMBERS
            or any(
                not isinstance(ref, BlobRef)
                or ref.media_type != "application/json"
                or not 0 < ref.size <= self.max_descriptor_bytes
                for ref in self.shards
            )
        ):
            raise ValueError("retained.unit.bounded-result-index-required")
        digests = tuple(ref.digest for ref in self.shards)
        if digests != tuple(sorted(set(digests))):
            raise ValueError("retained.unit.canonical-result-index-required")
        if (
            sum(ref.size for ref in self.shards) > self.max_descriptor_total_bytes
            or len(canonical_json_bytes(self.to_dict())) > self.max_index_bytes
            or self.metadata_memory_bound > self.max_metadata_memory_bytes
        ):
            raise ValueError("retained.unit.result-metadata-policy-exceeded")

    @property
    def metadata_memory_bound(self):
        return (
            16
            * (
                sum(ref.size for ref in self.shards)
                + self.max_descriptor_bytes
                + self.max_index_bytes
            )
            + 4096 * self.unit_count
            + 4096 * len(self.shards)
        )

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "release_authorized": False,
            **{key: getattr(self, key).uri for key in self.IDENTITY_FIELDS},
            "shards": [ref.to_dict() for ref in self.shards],
            "unit_count": self.unit_count,
            "archive_bytes": self.archive_bytes,
            **{key: getattr(self, key) for key in self.POLICY_FIELDS},
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "authority_mode",
                    "release_authorized",
                    "shards",
                    "unit_count",
                    "archive_bytes",
                    *cls.IDENTITY_FIELDS,
                    *cls.POLICY_FIELDS,
                }
            ),
        )
        if (
            data["authority_mode"] != "retained-project"
            or data["release_authorized"] is not False
        ):
            raise ValueError("retained.unit.original-authority-required")
        refs = data["shards"]
        if type(refs) is not list or not 1 <= len(refs) <= MAX_TREE_SHARDS:
            raise ValueError("retained.unit.bounded-result-index-required")
        return cls(
            *(ContentIdentity.parse_uri(data[key]) for key in cls.IDENTITY_FIELDS),
            tuple(BlobRef.from_dict(ref) for ref in refs),
            data["unit_count"],
            data["archive_bytes"],
            *(data[key] for key in cls.POLICY_FIELDS),
        )
