"""Reopen an exact metadata generation without repairing or creating CAS state.

Saved source descriptors are historical observations. This check proves the
complete deterministic page/metadata byte closure only; an enclosing freshness
owner must separately recapture the actual source and inspect physical output.
"""

from __future__ import annotations

import hashlib
import json

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.storage import FileSystemCAS

from .html_tree_generation import stage_html_tree_generation
from .native_tree_shards import read_native_tree


class _ExistingGenerationCAS(FileSystemCAS):
    """Reuse canonical CAS reads; every staging write becomes exact comparison."""

    def __init__(self, root):
        super().__init__(root, create=False)

    def put_bytes(self, content, *, media_type="application/octet-stream"):
        reference = BlobRef(
            hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
        )
        if self.get_bytes(reference) != content:
            raise ValueError("html.tree.existing-generation-bytes-differ")
        return reference

    def put_file(self, source, *, media_type="application/octet-stream"):
        raise ValueError("html.tree.generation-check-cannot-write-files")

    def _store_stream(self, reference, reader):
        raise ValueError("html.tree.generation-check-cannot-write-streams")


def verify_staged_html_tree_generation(reference, request, store):
    """Verify every declared page and metadata descendant, never adopt owners.

    Regenerating expected bytes through a comparison-only CAS rejects missing,
    edited, duplicate, foreign, cyclic or partial metadata closure. The bounded
    canonical source descriptor reader owns composition and alias resolution.
    No source payload is read or presented as an actual current input here.
    """
    if (
        not isinstance(reference, BlobRef)
        or not isinstance(request, HtmlTreeRenderRequest)
        or not isinstance(store, FileSystemCAS)
        or reference.media_type != "application/json"
        or not 0 < reference.size <= request.limits.max_index_bytes
    ):
        raise ValueError("html.tree.bounded-generation-read-context-required")
    raw = store.get_bytes(reference)
    manifest = json.loads(raw)
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema",
            "request_identity",
            "source_index",
            "projection_root",
            "root_page",
            "root_page_path",
            "page_shards",
            "page_count",
            "page_bytes",
            "directory_count",
            "source_member_count",
            "metadata_bytes_before_manifest",
        }
        or manifest["schema"] != "literate-ai/retained-tree-html-generation@1"
        or manifest["request_identity"] != request.identity.uri
        or canonical_json_bytes(manifest) != raw
    ):
        raise ValueError("html.tree.exact-generation-manifest-required")
    source = BlobRef.from_dict(manifest["source_index"])
    limits = request.limits
    tree = read_native_tree(
        source,
        store,
        expected_identity=ContentIdentity.parse_uri(source.identity),
        limits=limits.source,
        maximum_descriptor_bytes=limits.max_descriptor_bytes,
        maximum_descriptor_total_bytes=limits.max_descriptor_total_bytes,
        maximum_index_bytes=limits.max_index_bytes,
        maximum_metadata_memory_bytes=limits.max_metadata_memory_bytes,
    )
    checked = stage_html_tree_generation(
        tree, request, _ExistingGenerationCAS(store.root)
    )
    if checked.manifest != reference:
        raise ValueError("html.tree.generation-complete-closure-mismatch")
    return checked
