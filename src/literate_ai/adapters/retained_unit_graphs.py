"""Prepare, retain and reopen bounded unit graph metadata without live authority."""

from __future__ import annotations

import hashlib

from literate_ai.adapters.native_tree_shards import _document
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.native_tree_shards import (
    MAX_DESCRIPTOR_BYTES,
    MAX_DESCRIPTOR_TOTAL_BYTES,
    MAX_INDEX_BYTES,
    MAX_METADATA_MEMORY_BYTES,
    MAX_SHARD_MEMBERS,
    MAX_TREE_SHARDS,
    ComposedNativeTree,
    _integer,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_unit_graphs import (
    RetainedUnitGraph,
    RetainedUnitGraphIndex,
    RetainedUnitGraphShard,
)
from literate_ai.contracts.retained_unit_partition import RetainedUnitPartition
from literate_ai.storage.cas import FileSystemCAS


def _policy(descriptor, total, index, memory):
    for name, value, maximum in (
        ("descriptor", descriptor, MAX_DESCRIPTOR_BYTES),
        ("descriptor-total", total, MAX_DESCRIPTOR_TOTAL_BYTES),
        ("index", index, MAX_INDEX_BYTES),
        ("memory", memory, MAX_METADATA_MEMORY_BYTES),
    ):
        _integer(value, 1, maximum, "unit-graph-" + name)


def prepare_retained_unit_graph(
    partition,
    tree_index,
    *,
    max_descriptor_bytes=MAX_DESCRIPTOR_BYTES,
    max_descriptor_total_bytes=MAX_DESCRIPTOR_TOTAL_BYTES,
    max_index_bytes=MAX_INDEX_BYTES,
    max_metadata_memory_bytes=1024**3,
):
    """Pure plan: no CAS writes, native processes or live admission claims."""
    if not isinstance(partition, RetainedUnitPartition):
        raise TypeError("retained.unit.typed-partition-required")
    _policy(
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    if (
        not isinstance(tree_index, BlobRef)
        or tree_index.identity != partition.tree.identity.uri
        or tree_index.media_type != "application/json"
        or tree_index.size != len(canonical_json_bytes(partition.tree.index.to_dict()))
    ):
        raise ValueError("retained.unit.exact-tree-index-binding-required")
    groups = {kind: [] for kind in RetainedUnitGraphIndex.COUNT_FIELDS}
    descriptors = []
    total = count = 0

    def memory(descriptor_bytes, rows, shards):
        estimate = (
            16 * (descriptor_bytes + max_descriptor_bytes + max_index_bytes)
            + 2048 * rows
            + 4096 * shards
        )
        if estimate > max_metadata_memory_bytes:
            raise ValueError("retained.unit.graph-metadata-memory-policy-exceeded")

    memory(0, 0, 0)
    for kind in groups:
        base = len(
            canonical_json_bytes(
                {"schema": RetainedUnitGraphShard.SCHEMA, "kind": kind, "records": []}
            )
        )
        current, size = [], base

        def flush(kind=kind, base=base):
            nonlocal current, size, total
            shard = RetainedUnitGraphShard(kind, tuple(current))
            content = canonical_json_bytes(shard.to_dict())
            if len(content) != size:
                raise ValueError("retained.unit.graph-descriptor-size-mismatch")
            total += size
            if (
                total > max_descriptor_total_bytes
                or len(descriptors) >= MAX_TREE_SHARDS
            ):
                raise ValueError(
                    "retained.unit.aggregate-graph-descriptor-bound-exceeded"
                )
            reference = BlobRef(
                hashlib.sha256(content).hexdigest(), size, media_type="application/json"
            )
            descriptors.append(shard)
            groups[kind].append((reference, shard))
            current, size = [], base

        for row in getattr(partition, kind):
            row_size = len(canonical_json_bytes(row.to_dict()))
            if base + row_size > max_descriptor_bytes:
                raise ValueError("retained.unit.single-graph-record-bound-exceeded")
            if current and (
                len(current) == MAX_SHARD_MEMBERS
                or size + row_size + 1 > max_descriptor_bytes
            ):
                flush()
            count += 1
            size += row_size + bool(current)
            if total + size > max_descriptor_total_bytes:
                raise ValueError(
                    "retained.unit.aggregate-graph-descriptor-bound-exceeded"
                )
            memory(total + size, count, len(descriptors) + 1)
            current.append(row)
        if current:
            flush()
        groups[kind].sort(key=lambda pair: pair[0].digest)
    index = RetainedUnitGraphIndex(
        tree_index,
        *(tuple(ref for ref, _ in groups[kind]) for kind in groups),
        *(len(getattr(partition, kind)) for kind in groups),
        partition.per_unit_limits,
        partition.max_units,
        partition.max_dependencies,
        partition.max_metadata_bytes,
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    return RetainedUnitGraph(
        index,
        partition.tree,
        tuple(shard for kind in groups for _, shard in groups[kind]),
    )


def retain_retained_unit_graph(graph, store):
    """Retain exact metadata only; production still requires a current live owner."""
    if not isinstance(graph, RetainedUnitGraph) or not isinstance(store, FileSystemCAS):
        raise TypeError("retained.unit.typed-graph-store-required")
    references = tuple(
        ref for kind in graph.index.COUNT_FIELDS for ref in getattr(graph.index, kind)
    )
    for shard, reference in zip(graph.shards, references, strict=True):
        if (
            store.put_manifest(shard.to_dict(), media_type="application/json")
            != reference
        ):
            raise ValueError("retained.unit.retained-graph-descriptor-mismatch")
    return store.put_manifest(graph.index.to_dict(), media_type="application/json")


def read_retained_unit_graph(
    reference,
    store,
    *,
    expected_identity,
    tree,
    per_unit_limits,
    max_units,
    max_dependencies,
    max_metadata_bytes,
    max_descriptor_bytes,
    max_descriptor_total_bytes,
    max_index_bytes,
    max_metadata_memory_bytes,
):
    """Rehash and bound each raw document before typed decoding/composition."""
    if (
        not isinstance(store, FileSystemCAS)
        or not isinstance(tree, ComposedNativeTree)
        or not isinstance(per_unit_limits, RetainedProjectLimits)
        or not isinstance(expected_identity, ContentIdentity)
        or not isinstance(reference, BlobRef)
        or reference.identity != expected_identity.uri
    ):
        raise ValueError("retained.unit.exact-graph-read-context-required")
    _policy(
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    _integer(max_units, 1, 1_000_000, "unit-count")
    _integer(max_dependencies, 1, 4_000_000, "unit-dependency-count")
    _integer(max_metadata_bytes, 1, MAX_DESCRIPTOR_TOTAL_BYTES, "unit-metadata-bytes")
    index = RetainedUnitGraphIndex.from_dict(
        _document(store, reference, max_index_bytes)
    )
    policy = {
        "max_units": max_units,
        "max_dependencies": max_dependencies,
        "max_metadata_bytes": max_metadata_bytes,
        "max_descriptor_bytes": max_descriptor_bytes,
        "max_descriptor_total_bytes": max_descriptor_total_bytes,
        "max_index_bytes": max_index_bytes,
        "max_metadata_memory_bytes": max_metadata_memory_bytes,
    }
    if (
        index.identity != expected_identity
        or any(getattr(index, name) > value for name, value in policy.items())
        or any(
            getattr(index.per_unit_limits, name) > getattr(per_unit_limits, name)
            for name in per_unit_limits.__slots__
        )
        or index.tree_index.identity != tree.identity.uri
    ):
        raise ValueError("retained.unit.graph-read-policy-mismatch")
    shards = []
    rows = metadata = 0
    for kind, count_field in index.COUNT_FIELDS.items():
        count = 0
        for ref in getattr(index, kind):
            value = _document(store, ref, index.max_descriptor_bytes)
            if type(value) is not dict or value.get("kind") != kind:
                raise ValueError("retained.unit.graph-record-kind-mismatch")
            raw_rows = value.get("records")
            if type(raw_rows) is not list or count + len(raw_rows) > getattr(
                index, count_field
            ):
                raise ValueError("retained.unit.actual-graph-count-exceeds-declaration")
            shard = RetainedUnitGraphShard.from_dict(value)
            count += len(shard.records)
            rows += len(shard.records)
            for row in shard.records:
                metadata += len(canonical_json_bytes(row.to_dict())) + 1
                if metadata > index.max_metadata_bytes:
                    raise ValueError("retained.unit.partition-metadata-bound-exceeded")
            # Index admission conservatively reserves every declared row and
            # descriptor already. Actual rows cannot exceed those reservations.
            if rows > index.unit_count + index.ownership_count + index.dependency_count:
                raise ValueError("retained.unit.actual-graph-count-exceeds-declaration")
            shards.append(shard)
        if count != getattr(index, count_field):
            raise ValueError("retained.unit.actual-graph-count-mismatch")
    return RetainedUnitGraph(index, tree, tuple(shards))
