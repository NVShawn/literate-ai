"""Pure directory commitments over canonical composed-tree observations.

This adapter neither walks a filesystem nor grants source or output custody.
Its immutable bytes are planning data; publication must reobserve the actual
source and independently verify the complete generated artifact closure.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import PurePosixPath

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.native_tree_shards import ComposedNativeTree


@dataclass(frozen=True, slots=True)
class HtmlTreeProjection:
    root: BlobRef
    records: tuple[tuple[BlobRef, bytes], ...]
    directory_count: int
    source_member_count: int
    page_count: int
    metadata_memory_bound: int


def project_html_tree(tree, request, *, reserved_memory_bytes=0):
    """Bind every child, including aliases and empty directories, without I/O."""
    if not isinstance(tree, ComposedNativeTree) or not isinstance(
        request, HtmlTreeRenderRequest
    ):
        raise TypeError("html.tree.typed-observation-and-request-required")
    policy = request.limits
    if (
        type(reserved_memory_bytes) is not int
        or not 0 <= reserved_memory_bytes <= policy.max_metadata_memory_bytes
    ):
        raise ValueError("html.tree.invalid-memory-reservation")
    source = tree.index
    if source.case_sensitive != request.case_sensitive:
        raise ValueError("html.tree.case-policy-mismatch")
    if (
        source.member_count > policy.source.max_entries
        or source.payload_bytes > policy.source.max_total_bytes
        or any(v.blob.size > policy.source.max_file_bytes for v in tree.files)
        or any(
            len(v.target.encode("utf-8")) > policy.source.max_file_bytes
            for v in tree.aliases
            if v.kind == "symlink"
        )
    ):
        raise ValueError("html.tree.source-policy-exceeded")
    descriptor_bytes = sum(ref.size for ref in source.shards)
    # Charge the existing canonical observation and the additional adjacency,
    # ordered-child and record bookkeeping before constructing the projection.
    # This is a conservative allocation policy, not a measured peak-RSS claim.
    baseline = (
        16 * (descriptor_bytes + source.max_descriptor_bytes + source.max_index_bytes)
        + 4096 * len(source.shards)
        + 4096 * (source.member_count + 1)
    )
    if baseline + reserved_memory_bytes > policy.max_metadata_memory_bytes:
        raise ValueError("html.tree.projection-metadata-policy-exceeded")
    if (
        descriptor_bytes > policy.max_descriptor_total_bytes
        or any(ref.size > policy.max_descriptor_bytes for ref in source.shards)
        or len(canonical_json_bytes(source.to_dict())) > policy.max_index_bytes
    ):
        raise ValueError("html.tree.source-descriptor-policy-exceeded")
    source_identity = tree.identity.uri
    request_identity = request.identity.uri
    directories = (".", *tree.directories)
    children = {path: [] for path in directories}
    for path in tree.directories:
        children[PurePosixPath(path).parent.as_posix()].append(
            (path, "directory", None, None)
        )
    for item in tree.files:
        children[PurePosixPath(item.path).parent.as_posix()].append(
            (item.path, "file", canonical_identity(item.to_dict()).uri, None)
        )
    for item in tree.aliases:
        children[PurePosixPath(item.path).parent.as_posix()].append(
            (item.path, item.kind, canonical_identity(item.to_dict()).uri, item.target)
        )
    if any(
        len(PurePosixPath(path).parts) > policy.max_depth
        for path in (
            *tree.directories,
            *(v.path for v in tree.files),
            *(v.path for v in tree.aliases),
        )
    ):
        raise ValueError("html.tree.depth-policy-exceeded")
    records = []
    commitments = {}
    retained_bytes = 0
    pages = 0

    def retain(value, *, index=False):
        nonlocal retained_bytes
        bound = policy.max_index_bytes if index else policy.max_descriptor_bytes
        if (
            reserved_memory_bytes
            + baseline
            + 16 * (retained_bytes + bound)
            + 4096 * (len(records) + 1)
            > policy.max_metadata_memory_bytes
        ):
            raise ValueError("html.tree.projection-metadata-policy-exceeded")
        content = canonical_json_bytes(value)
        if len(content) > bound:
            raise ValueError("html.tree.projection-record-policy-exceeded")
        if retained_bytes + len(content) > policy.max_descriptor_total_bytes:
            raise ValueError("html.tree.projection-total-policy-exceeded")
        if (
            reserved_memory_bytes
            + baseline
            + 16 * (retained_bytes + len(content))
            + 4096 * (len(records) + 1)
            > policy.max_metadata_memory_bytes
        ):
            raise ValueError("html.tree.projection-metadata-policy-exceeded")
        ref = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/json",
        )
        retained_bytes += len(content)
        records.append((ref, content))
        return ref

    # Child directory commitments exist before their parents. The parent
    # navigation coordinate is not a parent digest, so no hash cycle is formed.
    for path in sorted(directories, key=lambda p: (-len(PurePosixPath(p).parts), p)):
        rows = sorted(children.pop(path))
        groups = []
        count = math.ceil(len(rows) / policy.max_children_per_group)
        page_count = max(1, math.ceil(count / policy.max_groups_per_page))
        if pages + page_count > policy.max_pages:
            raise ValueError("html.tree.page-count-policy-exceeded")
        pages += page_count
        for start in range(0, len(rows), policy.max_children_per_group):
            group = []
            for name, kind, identity, target in rows[
                start : start + policy.max_children_per_group
            ]:
                group.append(
                    {
                        "coordinate": name,
                        "kind": kind,
                        "target": target,
                        "identity": commitments[name].identity
                        if kind == "directory"
                        else identity,
                    }
                )
            groups.append(
                retain(
                    {
                        "schema": "literate-ai/retained-tree-html-child-group@1",
                        "directory": path,
                        "ordinal": len(groups),
                        "children": group,
                    }
                )
            )
        commitments[path] = retain(
            {
                "schema": "literate-ai/retained-tree-html-directory@1",
                "coordinate": path,
                "source_identity": source_identity,
                "request_identity": request_identity,
                "child_count": len(rows),
                "page_count": page_count,
                "groups": [ref.to_dict() for ref in groups],
            },
            index=True,
        )
    return HtmlTreeProjection(
        commitments["."],
        tuple(records),
        len(directories),
        source.member_count,
        pages,
        baseline + 16 * retained_bytes + 4096 * len(records),
    )
