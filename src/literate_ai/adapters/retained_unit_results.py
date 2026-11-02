"""Retain and reopen finite result indexes; no decoded value is a producer."""

from __future__ import annotations

import hashlib

from literate_ai.adapters.freebsd_composed_tree_packaging import (
    FreeBsdComposedPackageSetProjection,
)
from literate_ai.adapters.native_tree_shards import _document
from literate_ai.adapters.retained_unit_graphs import _policy
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.native_tree_shards import (
    MAX_DESCRIPTOR_BYTES,
    MAX_DESCRIPTOR_TOTAL_BYTES,
    MAX_INDEX_BYTES,
    MAX_SHARD_MEMBERS,
    MAX_TREE_SHARDS,
)
from literate_ai.contracts.retained_unit_packages import RetainedUnitPackageResult
from literate_ai.contracts.retained_unit_results import (
    RetainedUnitPackageResultShard,
    RetainedUnitPackageSetResult,
)
from literate_ai.storage.cas import FileSystemCAS


def _reference(content):
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type="application/json"
    )


def _context(projection):
    if not isinstance(projection, FreeBsdComposedPackageSetProjection):
        raise TypeError("retained.unit.typed-native-projection-required")
    return (
        projection.set_plan.identity,
        projection.graph.identity,
        projection.set_plan.packager_identity,
        projection.set_plan.tool_identity,
    )


def _require_result(projection, result, context):
    if not isinstance(result, RetainedUnitPackageResult):
        raise TypeError("retained.unit.typed-archive-result-required")
    plan = projection.unit_plan(result.definition_identity)
    if (
        result.unit_plan_identity,
        result.package_set_plan_identity,
        result.graph_identity,
        result.packager_identity,
        result.tool_identity,
    ) != (
        plan.identity,
        *context,
    ) or result.archive.size > projection.set_plan.max_archive_bytes:
        raise ValueError("retained.unit.result-context-or-archive-budget-differs")


def _require_set(projection, index):
    if not isinstance(projection, FreeBsdComposedPackageSetProjection):
        raise TypeError("retained.unit.typed-native-projection-required")
    if (
        index.package_set_plan_identity,
        index.graph_identity,
        index.packager_identity,
        index.tool_identity,
        index.unit_count,
    ) != (
        projection.set_plan.identity,
        projection.graph.identity,
        projection.set_plan.packager_identity,
        projection.set_plan.tool_identity,
        projection.graph.index.unit_count,
    ) or index.archive_bytes > projection.set_plan.max_total_archive_bytes:
        raise ValueError("retained.unit.set-result-context-or-budget-differs")
    if (
        index.archive_bytes
        + sum(ref.size for ref in index.shards)
        + len(canonical_json_bytes(index.to_dict()))
        > projection.set_plan.max_transport_bytes
    ):
        raise ValueError("retained.unit.result-transport-budget-exceeded")


def prepare_retained_unit_results(
    projection,
    results,
    *,
    max_descriptor_bytes=MAX_DESCRIPTOR_BYTES,
    max_descriptor_total_bytes=MAX_DESCRIPTOR_TOTAL_BYTES,
    max_index_bytes=MAX_INDEX_BYTES,
    max_metadata_memory_bytes=1024**3,
):
    """Pure plan over assertions; does not inspect archives or grant admission."""
    if not isinstance(projection, FreeBsdComposedPackageSetProjection):
        raise TypeError("retained.unit.typed-native-projection-required")
    _policy(
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    if type(results) is not tuple or len(results) != projection.graph.index.unit_count:
        raise ValueError("retained.unit.complete-set-results-required")

    def memory(size, count, shards):
        if (
            16 * (size + max_descriptor_bytes + max_index_bytes)
            + 4096 * count
            + 4096 * shards
            > max_metadata_memory_bytes
        ):
            raise ValueError("retained.unit.result-metadata-memory-policy-exceeded")

    memory(0, len(results), 0)
    context = _context(projection)
    base = len(
        canonical_json_bytes(
            {"schema": RetainedUnitPackageResultShard.SCHEMA, "results": []}
        )
    )
    chunks, current, size, total, archive_bytes, previous = [], [], base, 0, 0, ""

    def flush():
        nonlocal current, size, total
        shard = RetainedUnitPackageResultShard(tuple(current))
        content = canonical_json_bytes(shard.to_dict())
        if len(content) != size or len(chunks) >= MAX_TREE_SHARDS:
            raise ValueError("retained.unit.result-shard-size-or-count-exceeded")
        total += size
        chunks.append((_reference(content), shard))
        current, size = [], base

    for result in results:
        _require_result(projection, result, context)
        identity = result.definition_identity.uri
        if identity <= previous:
            raise ValueError(
                "retained.unit.canonical-complete-result-membership-required"
            )
        previous = identity
        archive_bytes += result.archive.size
        if archive_bytes > min(
            projection.set_plan.max_total_archive_bytes,
            projection.set_plan.max_transport_bytes,
        ):
            raise ValueError("retained.unit.result-aggregate-archive-budget-exceeded")
        row_size = len(canonical_json_bytes(result.to_dict()))
        if base + row_size > max_descriptor_bytes:
            raise ValueError("retained.unit.single-result-descriptor-bound-exceeded")
        if current and (
            len(current) == MAX_SHARD_MEMBERS
            or size + row_size + 1 > max_descriptor_bytes
        ):
            flush()
        size += row_size + bool(current)
        if total + size > max_descriptor_total_bytes:
            raise ValueError("retained.unit.aggregate-result-descriptor-bound-exceeded")
        memory(total + size, len(results), len(chunks) + 1)
        current.append(result)
    if current:
        flush()
    chunks.sort(key=lambda pair: pair[0].digest)
    index = RetainedUnitPackageSetResult(
        projection.set_plan.identity,
        projection.graph.identity,
        projection.set_plan.packager_identity,
        projection.set_plan.tool_identity,
        tuple(ref for ref, _ in chunks),
        len(results),
        archive_bytes,
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    _require_set(projection, index)
    return index, tuple(shard for _, shard in chunks)


def retain_retained_unit_results(projection, results, store, **policy):
    """Retain bounded assertions only after the entire metadata plan passes."""
    if not isinstance(store, FileSystemCAS):
        raise TypeError("retained.unit.filesystem-store-required")
    index, shards = prepare_retained_unit_results(projection, results, **policy)
    for reference, shard in zip(index.shards, shards, strict=True):
        if (
            store.put_manifest(shard.to_dict(), media_type="application/json")
            != reference
        ):
            raise ValueError("retained.unit.result-shard-identity-changed")
    return store.put_manifest(index.to_dict(), media_type="application/json")


def read_retained_unit_result_index(
    reference,
    store,
    *,
    max_descriptor_bytes=MAX_DESCRIPTOR_BYTES,
    max_descriptor_total_bytes=MAX_DESCRIPTOR_TOTAL_BYTES,
    max_index_bytes=MAX_INDEX_BYTES,
    max_metadata_memory_bytes=1024**3,
):
    """Read only the bounded root assertion, without retrieving any shard."""
    _policy(
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    if not isinstance(store, FileSystemCAS):
        raise TypeError("retained.unit.filesystem-store-required")
    if (
        not isinstance(reference, BlobRef)
        or 16 * reference.size > max_metadata_memory_bytes
    ):
        raise ValueError("retained.unit.result-index-memory-policy-exceeded")
    index = RetainedUnitPackageSetResult.from_dict(
        _document(store, reference, max_index_bytes)
    )
    if any(
        getattr(index, name) > maximum
        for name, maximum in zip(
            index.POLICY_FIELDS,
            (
                max_descriptor_bytes,
                max_descriptor_total_bytes,
                max_index_bytes,
                max_metadata_memory_bytes,
            ),
            strict=True,
        )
    ):
        raise ValueError("retained.unit.result-reader-policy-exceeded")
    return index


def read_retained_unit_results(
    projection,
    reference,
    store,
    *,
    max_descriptor_bytes=MAX_DESCRIPTOR_BYTES,
    max_descriptor_total_bytes=MAX_DESCRIPTOR_TOTAL_BYTES,
    max_index_bytes=MAX_INDEX_BYTES,
    max_metadata_memory_bytes=1024**3,
):
    """Rehash indexed assertions under caller limits; inspect archive bytes later."""
    context = _context(projection)
    index = read_retained_unit_result_index(
        reference,
        store,
        max_descriptor_bytes=max_descriptor_bytes,
        max_descriptor_total_bytes=max_descriptor_total_bytes,
        max_index_bytes=max_index_bytes,
        max_metadata_memory_bytes=max_metadata_memory_bytes,
    )
    _require_set(projection, index)
    results, seen, archive_bytes = [], set(), 0
    for ref in index.shards:
        if len(results) >= index.unit_count or archive_bytes >= index.archive_bytes:
            raise ValueError("retained.unit.result-actual-count-exceeds-index")
        shard = RetainedUnitPackageResultShard.from_dict(
            _document(store, ref, index.max_descriptor_bytes)
        )
        if len(results) + len(shard.results) > index.unit_count:
            raise ValueError("retained.unit.result-actual-count-exceeds-index")
        for result in shard.results:
            _require_result(projection, result, context)
            if result.definition_identity in seen:
                raise ValueError("retained.unit.duplicate-result-definition")
            archive_bytes += result.archive.size
            if archive_bytes > index.archive_bytes:
                raise ValueError("retained.unit.result-actual-bytes-exceed-index")
            seen.add(result.definition_identity)
            results.append(result)
    if len(results) != index.unit_count or archive_bytes != index.archive_bytes:
        raise ValueError("retained.unit.result-index-totals-differ")
    results.sort(key=lambda result: result.definition_identity.uri)
    return index, tuple(results)
