"""Prepare/reopen bounded canonical tree descriptors, never admission owners."""

from __future__ import annotations

import hashlib
import heapq
import json

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.native_tree_shards import (
    MAX_DESCRIPTOR_BYTES,
    MAX_DESCRIPTOR_TOTAL_BYTES,
    MAX_INDEX_BYTES,
    MAX_SHARD_MEMBERS,
    MAX_TREE_MEMBERS,
    MAX_TREE_SHARDS,
    ComposedNativeTree,
    NativeTreeIndex,
    NativeTreeShard,
    validate_native_tree_policy,
)
from literate_ai.storage import FileSystemCAS


def _ordered(values, group):
    previous = None
    for value in values:
        name = value if group == 0 else value.path
        if previous is not None and name <= previous:
            raise ValueError("tree.shards.canonical-input-order-required")
        previous = name
        yield name, group, value


def prepare_native_tree(
    directories,
    files,
    aliases,
    *,
    limits,
    case_sensitive=False,
    max_descriptor_bytes=MAX_DESCRIPTOR_BYTES,
    max_descriptor_total_bytes=MAX_DESCRIPTOR_TOTAL_BYTES,
    max_index_bytes=MAX_INDEX_BYTES,
    max_metadata_memory_bytes=1024 * 1024 * 1024,
    reserved_memory_bytes=0,
):
    """Pure planning: compute shards/references without CAS or host-path writes."""
    validate_native_tree_policy(
        limits,
        case_sensitive,
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    if (
        type(reserved_memory_bytes) is not int
        or not 0 <= reserved_memory_bytes <= max_metadata_memory_bytes
    ):
        raise ValueError("tree.shards.invalid-memory-reservation")
    if reserved_memory_bytes and (
        reserved_memory_bytes + 16 * (max_descriptor_bytes + max_index_bytes)
        >= max_metadata_memory_bytes
    ):
        raise ValueError("tree.shards.metadata-memory-policy-exceeded")
    if any(type(group) is not tuple for group in (directories, files, aliases)):
        raise TypeError("tree.shards.canonical-input-tuples-required")
    base_size = len(
        canonical_json_bytes(
            {
                "schema": NativeTreeShard.SCHEMA,
                "directories": [],
                "files": [],
                "aliases": [],
                "case_sensitive": case_sensitive,
            }
        )
    )
    groups = [[], [], []]
    shard_size, shard_count = base_size, 0
    descriptors, descriptor_total = [], 0
    count = total = 0

    def flush():
        nonlocal groups, shard_size, shard_count, descriptor_total
        shard = NativeTreeShard(*(tuple(v) for v in groups), case_sensitive)
        content = canonical_json_bytes(shard.to_dict())
        if len(content) != shard_size or len(content) > max_descriptor_bytes:
            raise ValueError("tree.shards.descriptor-byte-bound-exceeded")
        descriptor_total += len(content)
        if (
            descriptor_total > max_descriptor_total_bytes
            or len(descriptors) >= MAX_TREE_SHARDS
        ):
            raise ValueError("tree.shards.descriptor-index-bound-exceeded")
        reference = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/json",
        )
        descriptors.append((reference, shard))
        groups = [[], [], []]
        shard_size, shard_count = base_size, 0

    streams = (_ordered(directories, 0), _ordered(files, 1), _ordered(aliases, 2))
    for _name, group, value in heapq.merge(*streams, key=lambda item: item[0]):
        if group == 1:
            total += value.blob.size
            if value.blob.size > limits.max_file_bytes:
                raise ValueError("tree.shards.file-byte-bound-exceeded")
        elif group == 2 and value.kind == "symlink":
            size = len(value.target.encode("utf-8"))
            total += size
            if size > limits.max_file_bytes:
                raise ValueError("tree.shards.alias-byte-bound-exceeded")
        count += 1
        if (
            count > min(limits.max_entries, MAX_TREE_MEMBERS)
            or total > limits.max_total_bytes
        ):
            raise ValueError("tree.shards.payload-bound-exceeded")
        wire = value if group == 0 else value.to_dict()
        size = len(canonical_json_bytes(wire))
        if base_size + size > max_descriptor_bytes:
            raise ValueError("tree.shards.single-entry-descriptor-bound-exceeded")
        additional = size + bool(groups[group])
        if shard_count and (
            shard_count == MAX_SHARD_MEMBERS
            or shard_size + additional > max_descriptor_bytes
        ):
            flush()
            additional = size
        estimate = (
            reserved_memory_bytes
            + 16
            * (
                descriptor_total
                + shard_size
                + additional
                + max_descriptor_bytes
                + max_index_bytes
            )
            + count * 2048
            + (len(descriptors) + 1) * 4096
        )
        if estimate > max_metadata_memory_bytes:
            raise ValueError("tree.shards.metadata-memory-policy-exceeded")
        groups[group].append(value)
        shard_count += 1
        shard_size += additional
    if shard_count:
        flush()
    descriptors.sort(key=lambda item: item[0].digest)
    index = NativeTreeIndex(
        tuple(v[0] for v in descriptors),
        count,
        total,
        limits,
        case_sensitive,
        max_descriptor_bytes,
        max_descriptor_total_bytes,
        max_index_bytes,
        max_metadata_memory_bytes,
    )
    return ComposedNativeTree(index, tuple(v[1] for v in descriptors))


def retain_native_tree(tree, store):
    """Retain exact descriptors; no producer, runtime or PACKAGE authority."""
    if not isinstance(tree, ComposedNativeTree) or not isinstance(store, FileSystemCAS):
        raise TypeError("tree.shards.typed-tree-and-filesystem-store-required")
    for shard, expected in zip(tree.shards, tree.index.shards, strict=True):
        if (
            store.put_manifest(shard.to_dict(), media_type="application/json")
            != expected
        ):
            raise ValueError("tree.shards.retained-descriptor-mismatch")
    return store.put_manifest(tree.index.to_dict(), media_type="application/json")


def _document(store, reference, maximum):
    if (
        not isinstance(reference, BlobRef)
        or reference.media_type != "application/json"
        or not 0 < reference.size <= maximum
    ):
        raise ValueError("tree.shards.raw-document-bound-exceeded")
    # FileSystemCAS checks actual regular-file size before collecting bytes and
    # hashes the bounded read through that same descriptor.
    content = store.get_bytes(reference)
    try:
        value = json.loads(content)
        if canonical_json_bytes(value) != content:
            raise ValueError("noncanonical descriptor")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("tree.shards.invalid-canonical-document") from exc
    return value


def read_native_tree(
    reference,
    store,
    *,
    expected_identity,
    limits,
    maximum_descriptor_bytes,
    maximum_descriptor_total_bytes,
    maximum_index_bytes,
    maximum_metadata_memory_bytes,
):
    """Rehash bounded descriptors before composing geometry; grant no authority."""
    if not isinstance(store, FileSystemCAS) or not isinstance(
        expected_identity, ContentIdentity
    ):
        raise TypeError("tree.shards.typed-read-context-required")
    validate_native_tree_policy(
        limits,
        False,
        maximum_descriptor_bytes,
        maximum_descriptor_total_bytes,
        maximum_index_bytes,
        maximum_metadata_memory_bytes,
    )
    if (
        not isinstance(reference, BlobRef)
        or reference.identity != expected_identity.uri
    ):
        raise ValueError("tree.shards.expected-index-identity-required")
    index = NativeTreeIndex.from_dict(_document(store, reference, maximum_index_bytes))
    if (
        index.identity != expected_identity
        or any(getattr(index.limits, n) > getattr(limits, n) for n in limits.__slots__)
        or index.max_descriptor_bytes > maximum_descriptor_bytes
        or index.max_descriptor_total_bytes > maximum_descriptor_total_bytes
        or index.max_index_bytes > maximum_index_bytes
        or index.max_metadata_memory_bytes > maximum_metadata_memory_bytes
    ):
        raise ValueError("tree.shards.read-policy-mismatch")
    shards = []
    count = total = descriptor_total = 0
    for ref in index.shards:
        shard = NativeTreeShard.from_dict(
            _document(store, ref, index.max_descriptor_bytes)
        )
        count += shard.member_count
        total += shard.payload_bytes
        descriptor_total += ref.size
        if count > index.member_count or total > index.payload_bytes:
            raise ValueError("tree.shards.declared-totals-exceeded")
        estimate = (
            16 * (descriptor_total + index.max_descriptor_bytes + index.max_index_bytes)
            + 2048 * count
            + 4096 * (len(shards) + 1)
        )
        if estimate > index.max_metadata_memory_bytes:
            raise ValueError("tree.shards.metadata-memory-policy-exceeded")
        shards.append(shard)
    return ComposedNativeTree(index, tuple(shards))
