"""Bounded graph descriptors for retained units; no decoded value grants admission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from ._validation import contract_fields
from .blobs import BlobRef
from .identity import canonical_json_bytes, contract_identity
from .native_tree_shards import (
    MAX_DESCRIPTOR_BYTES,
    MAX_DESCRIPTOR_TOTAL_BYTES,
    MAX_INDEX_BYTES,
    MAX_METADATA_MEMORY_BYTES,
    MAX_SHARD_MEMBERS,
    MAX_TREE_MEMBERS,
    MAX_TREE_SHARDS,
    ComposedNativeTree,
    _integer,
)
from .retained_project import RetainedProjectLimits
from .retained_unit_packages import (
    RetainedUnitDependency,
    RetainedUnitOwnership,
    RetainedUnitPackageDefinition,
)
from .retained_unit_partition import RetainedUnitPartition

GRAPH_RECORD_TYPES = {
    "definitions": RetainedUnitPackageDefinition,
    "ownership": RetainedUnitOwnership,
    "dependencies": RetainedUnitDependency,
}


def graph_record_key(value):
    if isinstance(value, RetainedUnitPackageDefinition):
        return (value.identity.uri,)
    if isinstance(value, RetainedUnitOwnership):
        return (value.path,)
    if isinstance(value, RetainedUnitDependency):
        return (
            value.source_unit_identity.uri,
            value.target_unit_identity.uri,
            value.kind,
        )
    raise TypeError("retained.unit.typed-graph-record-required")


@dataclass(frozen=True, slots=True)
class RetainedUnitGraphShard:
    kind: str
    records: tuple

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-graph-shard@1"

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in GRAPH_RECORD_TYPES:
            raise ValueError("retained.unit.graph-record-kind-invalid")
        if (
            type(self.records) is not tuple
            or not 1 <= len(self.records) <= MAX_SHARD_MEMBERS
            or any(
                not isinstance(value, GRAPH_RECORD_TYPES[self.kind])
                for value in self.records
            )
        ):
            raise ValueError("retained.unit.bounded-typed-graph-records-required")
        keys = tuple(graph_record_key(value) for value in self.records)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("retained.unit.unique-canonical-graph-records-required")
        if len(canonical_json_bytes(self.to_dict())) > MAX_DESCRIPTOR_BYTES:
            raise ValueError("retained.unit.graph-descriptor-byte-bound-exceeded")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "records": [value.to_dict() for value in self.records],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"kind", "records"}),
        )
        kind, rows = data["kind"], data["records"]
        if (
            not isinstance(kind, str)
            or kind not in GRAPH_RECORD_TYPES
            or type(rows) is not list
            or not 1 <= len(rows) <= MAX_SHARD_MEMBERS
        ):
            raise ValueError("retained.unit.bounded-typed-graph-records-required")
        return cls(kind, tuple(GRAPH_RECORD_TYPES[kind].from_dict(row) for row in rows))


@dataclass(frozen=True, slots=True)
class RetainedUnitGraphIndex:
    tree_index: BlobRef
    definitions: tuple[BlobRef, ...]
    ownership: tuple[BlobRef, ...]
    dependencies: tuple[BlobRef, ...]
    unit_count: int
    ownership_count: int
    dependency_count: int
    per_unit_limits: RetainedProjectLimits
    max_units: int
    max_dependencies: int
    max_metadata_bytes: int
    max_descriptor_bytes: int = MAX_DESCRIPTOR_BYTES
    max_descriptor_total_bytes: int = MAX_DESCRIPTOR_TOTAL_BYTES
    max_index_bytes: int = MAX_INDEX_BYTES
    max_metadata_memory_bytes: int = 1024**3

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-graph-index@1"
    COUNT_FIELDS: ClassVar[dict[str, str]] = {
        "definitions": "unit_count",
        "ownership": "ownership_count",
        "dependencies": "dependency_count",
    }
    POLICY_FIELDS: ClassVar[tuple[str, ...]] = (
        "max_units",
        "max_dependencies",
        "max_metadata_bytes",
        "max_descriptor_bytes",
        "max_descriptor_total_bytes",
        "max_index_bytes",
        "max_metadata_memory_bytes",
    )

    def __post_init__(self):
        if not isinstance(self.per_unit_limits, RetainedProjectLimits):
            raise TypeError("retained.unit.typed-per-unit-limits-required")
        for name, maximum in (
            ("max_units", 1_000_000),
            ("max_dependencies", 4_000_000),
            ("max_metadata_bytes", MAX_DESCRIPTOR_TOTAL_BYTES),
            ("max_descriptor_bytes", MAX_DESCRIPTOR_BYTES),
            ("max_descriptor_total_bytes", MAX_DESCRIPTOR_TOTAL_BYTES),
            ("max_index_bytes", MAX_INDEX_BYTES),
            ("max_metadata_memory_bytes", MAX_METADATA_MEMORY_BYTES),
        ):
            _integer(getattr(self, name), 1, maximum, "unit-" + name)
        for name, minimum, maximum in (
            ("unit_count", 1, self.max_units),
            ("ownership_count", 1, MAX_TREE_MEMBERS),
            ("dependency_count", 0, self.max_dependencies),
        ):
            _integer(getattr(self, name), minimum, maximum, "unit-" + name)
        if (
            not isinstance(self.tree_index, BlobRef)
            or self.tree_index.media_type != "application/json"
            or not 0 < self.tree_index.size <= MAX_INDEX_BYTES
        ):
            raise ValueError("retained.unit.bounded-tree-index-reference-required")
        references = []
        for kind, count_name in self.COUNT_FIELDS.items():
            group, count = getattr(self, kind), getattr(self, count_name)
            if (
                type(group) is not tuple
                or len(group) > min(MAX_TREE_SHARDS, count)
                or bool(group) != bool(count)
            ):
                raise ValueError("retained.unit.bounded-graph-reference-index-required")
            if count > len(group) * MAX_SHARD_MEMBERS:
                raise ValueError("retained.unit.graph-count-exceeds-shard-capacity")
            if any(
                not isinstance(ref, BlobRef)
                or ref.media_type != "application/json"
                or not 0 < ref.size <= self.max_descriptor_bytes
                for ref in group
            ):
                raise ValueError(
                    "retained.unit.bounded-graph-descriptor-reference-required"
                )
            digests = tuple(ref.digest for ref in group)
            if digests != tuple(sorted(set(digests))):
                raise ValueError(
                    "retained.unit.unique-canonical-graph-references-required"
                )
            references.extend(group)
        if (
            len(references) > MAX_TREE_SHARDS
            or len({ref.digest for ref in references}) != len(references)
            or sum(ref.size for ref in references) > self.max_descriptor_total_bytes
        ):
            raise ValueError("retained.unit.aggregate-graph-descriptor-bound-exceeded")
        if len(canonical_json_bytes(self.to_dict())) > self.max_index_bytes:
            raise ValueError("retained.unit.graph-index-byte-bound-exceeded")
        if self.metadata_memory_bound > self.max_metadata_memory_bytes:
            raise ValueError("retained.unit.graph-metadata-memory-policy-exceeded")

    @property
    def metadata_memory_bound(self):
        references = (*self.definitions, *self.ownership, *self.dependencies)
        return (
            16
            * (
                sum(ref.size for ref in references)
                + self.max_descriptor_bytes
                + self.max_index_bytes
            )
            + 2048 * (self.unit_count + self.ownership_count + self.dependency_count)
            + 4096 * len(references)
        )

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "tree_index": self.tree_index.to_dict(),
            **{
                kind: [ref.to_dict() for ref in getattr(self, kind)]
                for kind in self.COUNT_FIELDS
            },
            **{name: getattr(self, name) for name in self.COUNT_FIELDS.values()},
            "per_unit_limits": self.per_unit_limits.to_dict(),
            **{name: getattr(self, name) for name in self.POLICY_FIELDS},
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "tree_index",
                    "per_unit_limits",
                    *cls.COUNT_FIELDS,
                    *cls.COUNT_FIELDS.values(),
                    *cls.POLICY_FIELDS,
                }
            ),
        )
        for kind in cls.COUNT_FIELDS:
            if type(data[kind]) is not list or len(data[kind]) > MAX_TREE_SHARDS:
                raise ValueError("retained.unit.bounded-graph-reference-index-required")
        return cls(
            BlobRef.from_dict(data["tree_index"]),
            *(
                tuple(BlobRef.from_dict(ref) for ref in data[kind])
                for kind in cls.COUNT_FIELDS
            ),
            *(data[name] for name in cls.COUNT_FIELDS.values()),
            RetainedProjectLimits.from_dict(data["per_unit_limits"]),
            *(data[name] for name in cls.POLICY_FIELDS),
        )


@dataclass(frozen=True, slots=True)
class RetainedUnitGraph:
    """Rehashed graph plus exact tree; still a mechanical, non-admitted value."""

    index: RetainedUnitGraphIndex
    tree: ComposedNativeTree
    shards: tuple[RetainedUnitGraphShard, ...]
    partition: RetainedUnitPartition = field(init=False, repr=False)

    def __post_init__(self):
        if (
            not isinstance(self.index, RetainedUnitGraphIndex)
            or not isinstance(self.tree, ComposedNativeTree)
            or type(self.shards) is not tuple
        ):
            raise TypeError("retained.unit.typed-graph-composition-required")
        tree_document = canonical_json_bytes(self.tree.index.to_dict())
        if (
            self.index.tree_index.identity != self.tree.identity.uri
            or self.index.tree_index.size != len(tree_document)
        ):
            raise ValueError("retained.unit.exact-tree-index-binding-required")
        references = tuple(
            (kind, ref)
            for kind in self.index.COUNT_FIELDS
            for ref in getattr(self.index, kind)
        )
        if len(self.shards) != len(references):
            raise ValueError("retained.unit.exact-graph-shard-membership-required")
        rows = {kind: [] for kind in self.index.COUNT_FIELDS}
        metadata = 0
        for shard, (kind, reference) in zip(self.shards, references, strict=True):
            if (
                not isinstance(shard, RetainedUnitGraphShard)
                or shard.kind != kind
                or shard.identity.digest != reference.digest
                or len(canonical_json_bytes(shard.to_dict())) != reference.size
            ):
                raise ValueError("retained.unit.graph-shard-reference-mismatch")
            if len(rows[kind]) + len(shard.records) > getattr(
                self.index, self.index.COUNT_FIELDS[kind]
            ):
                raise ValueError("retained.unit.actual-graph-count-exceeds-declaration")
            for row in shard.records:
                metadata += len(canonical_json_bytes(row.to_dict())) + 1
                if metadata > self.index.max_metadata_bytes:
                    raise ValueError("retained.unit.partition-metadata-bound-exceeded")
            rows[kind].extend(shard.records)
        for kind, count_name in self.index.COUNT_FIELDS.items():
            if len(rows[kind]) != getattr(self.index, count_name):
                raise ValueError("retained.unit.actual-graph-count-mismatch")
            rows[kind].sort(key=graph_record_key)
        object.__setattr__(
            self,
            "partition",
            RetainedUnitPartition(
                self.tree,
                *(tuple(rows[kind]) for kind in self.index.COUNT_FIELDS),
                self.index.per_unit_limits,
                self.index.max_units,
                self.index.max_dependencies,
                self.index.max_metadata_bytes,
            ),
        )

    @property
    def identity(self):
        return self.index.identity
