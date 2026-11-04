"""Retain bounded tree-page generations in an explicitly supplied content store.

CAS writes are append-only staging, not publication. The returned manifest is a
description of bytes, never source currentness, output ownership or permission to
replace an index. Publication and physical namespace verification remain separate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.native_tree_shards import (
    MAX_SHARD_MEMBERS,
    ComposedNativeTree,
)
from literate_ai.storage import FileSystemCAS

from .html_tree_emitter import directory_page_path, emit_html_tree_pages
from .html_tree_projection import project_html_tree
from .native_tree_shards import retain_native_tree


@dataclass(frozen=True, slots=True)
class StagedHtmlTreeGeneration:
    manifest: BlobRef
    page_count: int
    page_bytes: int
    metadata_bytes: int
    metadata_record_count: int

    def __post_init__(self):
        require_html_tree_generation_receipt(self)


def require_html_tree_generation_receipt(value):
    """Validate descriptive count types before they control any allocation."""
    if (
        not isinstance(value, StagedHtmlTreeGeneration)
        or not isinstance(value.manifest, BlobRef)
        or value.manifest.media_type != "application/json"
        or value.manifest.size <= 0
        or any(
            type(count) is not int or not 0 < count <= 2**63 - 1
            for count in (
                value.page_count,
                value.page_bytes,
                value.metadata_bytes,
                value.metadata_record_count,
            )
        )
    ):
        raise ValueError("html.tree.invalid-staged-generation-receipt")


def stage_html_tree_generation(tree, request, store, *, reserved_memory_bytes=0):
    """Retain the complete page closure without retaining all rendered pages.

    A page shard contains only bounded path/byte/directory commitments. The root
    manifest commits to every shard and the complete directory projection. No
    page embeds its generation manifest, so there is no self-referential digest.
    An interrupted call leaves only unreferenced CAS blobs, no visible index.
    """
    if (
        not isinstance(tree, ComposedNativeTree)
        or not isinstance(request, HtmlTreeRenderRequest)
        or not isinstance(store, FileSystemCAS)
    ):
        raise TypeError("html.tree.typed-generation-staging-required")
    limits = request.limits
    if (
        type(reserved_memory_bytes) is not int
        or not 0 <= reserved_memory_bytes <= limits.max_metadata_memory_bytes
    ):
        raise ValueError("html.tree.invalid-memory-reservation")
    # These structures remain live while the emitter holds its projection and
    # page. Reserve the bounded shard rows, reference index, serialization and
    # descriptor buffers before invoking that producer, not after it yields.
    staging_memory = 16 * (limits.max_descriptor_bytes + limits.max_index_bytes)
    if reserved_memory_bytes + staging_memory > limits.max_metadata_memory_bytes:
        raise ValueError("html.tree.generation-metadata-memory-policy-exceeded")
    if any(
        getattr(tree.index.limits, name) > getattr(limits.source, name)
        for name in limits.source.__slots__
    ) or any(
        getattr(tree.index, name) > getattr(limits, name)
        for name in (
            "max_descriptor_bytes",
            "max_descriptor_total_bytes",
            "max_index_bytes",
            "max_metadata_memory_bytes",
        )
    ):
        raise ValueError("html.tree.generation-source-policy-envelope-exceeded")
    metadata_bytes = 0
    metadata_record_count = 0
    page_bytes = 0
    page_count = 0
    references = []
    reference_index_bytes = 2  # Canonical empty array brackets.
    rows = []
    row_bytes = 0
    root_page = None

    def retain_metadata(content, *, index=False):
        nonlocal metadata_bytes, metadata_record_count
        bound = limits.max_index_bytes if index else limits.max_descriptor_bytes
        if len(content) > bound:
            raise ValueError("html.tree.generation-metadata-record-exceeded")
        if metadata_bytes + len(content) > limits.max_descriptor_total_bytes:
            raise ValueError("html.tree.generation-metadata-total-exceeded")
        if (
            page_bytes + metadata_bytes + len(content)
            > limits.max_retained_generation_bytes
        ):
            raise ValueError("html.tree.generation-storage-exceeded")
        reference = store.put_bytes(content, media_type="application/json")
        metadata_bytes += len(content)
        metadata_record_count += 1
        return reference

    def shard_document(values):
        return {
            "schema": "literate-ai/retained-tree-html-page-shard@1",
            "request_identity": request.identity.uri,
            "ordinal": len(references),
            "pages": values,
        }

    def flush():
        nonlocal rows, row_bytes, reference_index_bytes
        content = canonical_json_bytes(shard_document(rows))
        predicted = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/json",
        )
        # The reference list alone must fit before another shard is written.
        # The complete manifest receives its exact independent bound below.
        next_index_bytes = (
            reference_index_bytes
            + len(canonical_json_bytes(predicted.to_dict()))
            + bool(references)
        )
        if next_index_bytes > limits.max_index_bytes:
            raise ValueError("html.tree.generation-reference-index-exceeded")
        reference = retain_metadata(content)
        if reference != predicted:
            raise ValueError("html.tree.generation-shard-reference-mismatch")
        references.append(reference)
        reference_index_bytes = next_index_bytes
        rows = []
        row_bytes = 0

    for page in emit_html_tree_pages(
        tree, request, reserved_memory_bytes=reserved_memory_bytes + staging_memory
    ):
        if (
            page_count >= limits.max_pages
            or page_bytes + len(page.content) > limits.max_total_generated_bytes
            or page_bytes + metadata_bytes + len(page.content)
            > limits.max_retained_generation_bytes
        ):
            raise ValueError("html.tree.generation-output-policy-exceeded")
        # Filename derivation is fixed; source labels never become output paths.
        if page.path != directory_page_path(page.directory, page.ordinal):
            raise ValueError("html.tree.generation-page-path-mismatch")
        reference = BlobRef(
            hashlib.sha256(page.content).hexdigest(),
            len(page.content),
            media_type="text/html",
        )
        row = {
            "path": page.path,
            "directory": page.directory,
            "ordinal": page.ordinal,
            "directory_commitment": page.commitment.to_dict(),
            "content": reference.to_dict(),
        }
        encoded_size = len(canonical_json_bytes(row))
        # Charge descriptor geometry before adding the next bounded row. The
        # empty document includes its current ordinal and request commitment.
        base = len(canonical_json_bytes(shard_document([])))
        if rows and (
            len(rows) >= MAX_SHARD_MEMBERS
            or base + row_bytes + len(rows) + encoded_size > limits.max_descriptor_bytes
        ):
            flush()
            base = len(canonical_json_bytes(shard_document([])))
        if base + encoded_size > limits.max_descriptor_bytes:
            raise ValueError("html.tree.generation-page-metadata-exceeded")
        pending = base + row_bytes + len(rows) + encoded_size
        if metadata_bytes + pending > limits.max_descriptor_total_bytes:
            raise ValueError("html.tree.generation-metadata-total-exceeded")
        if (
            page_bytes + len(page.content) + metadata_bytes + pending
            > limits.max_retained_generation_bytes
        ):
            raise ValueError("html.tree.generation-storage-exceeded")
        if store.put_bytes(page.content, media_type="text/html") != reference:
            raise ValueError("html.tree.generation-page-reference-mismatch")
        rows.append(row)
        row_bytes += encoded_size
        page_bytes += len(page.content)
        page_count += 1
        if page.directory == "." and page.ordinal == 0:
            if root_page is not None:
                raise ValueError("html.tree.generation-duplicate-root-page")
            root_page = reference
    if rows:
        flush()
    if root_page is None:
        raise ValueError("html.tree.generation-root-page-required")
    # The for-loop target otherwise keeps the final rendered page alive during
    # the second projection, even after its producer has fully exhausted.
    del page

    # The emitter's projection has been released before this second pure pass.
    # It does not recapture or reread any source file. Persist its actual complete
    # metadata DAG now without keeping two projection allocations concurrently.
    projection = project_html_tree(
        tree, request, reserved_memory_bytes=reserved_memory_bytes + staging_memory
    )
    if projection.page_count != page_count:
        raise ValueError("html.tree.generation-page-count-mismatch")
    for expected, content in projection.records:
        directory_record = (
            json.loads(content)["schema"]
            == "literate-ai/retained-tree-html-directory@1"
        )
        if retain_metadata(content, index=directory_record) != expected:
            raise ValueError("html.tree.generation-projection-reference-mismatch")
    # Retain canonical source descriptors, not another copy of source payloads.
    # Charge them before invoking the canonical descriptor storage boundary.
    source_bytes = sum(ref.size for ref in tree.index.shards)
    source_index = canonical_json_bytes(tree.index.to_dict())
    if (
        metadata_bytes + source_bytes + len(source_index)
        > limits.max_descriptor_total_bytes
        or page_bytes + metadata_bytes + source_bytes + len(source_index)
        > limits.max_retained_generation_bytes
    ):
        raise ValueError("html.tree.generation-source-descriptor-policy-exceeded")
    source_reference = retain_native_tree(tree, store)
    metadata_bytes += source_bytes + len(source_index)
    metadata_record_count += len(tree.index.shards) + 1
    manifest = {
        "schema": "literate-ai/retained-tree-html-generation@1",
        "request_identity": request.identity.uri,
        "source_index": source_reference.to_dict(),
        "projection_root": projection.root.to_dict(),
        "root_page": root_page.to_dict(),
        "root_page_path": directory_page_path("."),
        "page_shards": [ref.to_dict() for ref in references],
        "page_count": page_count,
        "page_bytes": page_bytes,
        "directory_count": projection.directory_count,
        "source_member_count": projection.source_member_count,
        "metadata_bytes_before_manifest": metadata_bytes,
    }
    result = retain_metadata(canonical_json_bytes(manifest), index=True)
    return StagedHtmlTreeGeneration(
        result, page_count, page_bytes, metadata_bytes, metadata_record_count
    )
