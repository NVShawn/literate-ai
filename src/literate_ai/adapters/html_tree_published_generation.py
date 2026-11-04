"""Reobserve self-contained published pages without an operator cache or writes.

This is a generation reader for the tree freshness boundary. It does not adopt
an output namespace, exclude source paths, recover a publisher or authorize an
index replacement. Historical descriptors are checked data, not current source.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.native_tree_shards import NativeTreeIndex
from literate_ai.storage import FileSystemCAS

from .html_tree_emitter import emit_html_tree_pages
from .html_tree_generation import stage_html_tree_generation
from .html_tree_generation_check import _ExistingGenerationCAS
from .html_tree_generation_files import observe_html_tree_generation_files
from .html_tree_output_initialization import _node, _read_at
from .native_tree_shards import _document, read_native_tree
from .retained_project_inputs import _root


class _PublishedGenerationCAS(FileSystemCAS):
    """A read-only adapter over actual generation files, never a writable CAS.

    FileSystemCAS's normal constructor creates or requires a blob namespace.
    Here every read instead uses held generation descriptors and exact bounded
    references. All inherited storage writes converge on the refused methods.
    No payload bytes or decoded tree are retained in this adapter.
    """

    def __init__(self, root, manifest, read, guard, limits):
        self.root = root
        self._manifest = manifest
        self._read = read
        self._guard = guard
        self._limits = limits
        self._pages = {}
        self._records = {}
        self._record_limit = 1

    def set_record_limit(self, maximum):
        if type(maximum) is not int or maximum < len(self._records):
            raise ValueError("html.tree.published-record-bound-invalid")
        self._record_limit = maximum

    def observe_page(self, page):
        self._guard()
        reference = BlobRef(
            hashlib.sha256(page.content).hexdigest(),
            len(page.content),
            media_type="text/html",
        )
        # Distinct pages are separately checked even if their bytes coincide.
        self._checked("root", page.path, reference)
        if reference.digest not in self._pages:
            if len(self._pages) >= self._limits.max_pages:
                raise ValueError("html.tree.published-page-bound-exceeded")
            self._pages[reference.digest] = page.path

    def _checked(self, group, name, reference):
        self._guard()
        key = group, name
        if key not in self._records and len(self._records) >= self._record_limit:
            raise ValueError("html.tree.published-record-bound-exceeded")
        signature, content = self._read(group, name, reference.size)
        record = signature, reference
        if (
            len(content) != reference.size
            or hashlib.sha256(content).hexdigest() != reference.digest
            or key in self._records
            and self._records[key] != record
        ):
            raise ValueError("html.tree.published-file-changed")
        self._records[key] = record
        self._guard()
        return content

    def get_bytes(self, reference):
        if not isinstance(reference, BlobRef) or reference.size <= 0:
            raise ValueError("html.tree.published-reference-required")
        if reference == self._manifest:
            group, name, maximum = (
                "root",
                "manifest.json",
                self._limits.max_index_bytes,
            )
        elif reference.media_type == "application/json":
            group, name, maximum = (
                "metadata",
                reference.digest + ".json",
                max(self._limits.max_descriptor_bytes, self._limits.max_index_bytes),
            )
        elif reference.media_type == "text/html":
            name = self._pages.get(reference.digest)
            if name is None:
                raise ValueError("html.tree.published-page-not-observed")
            group, maximum = "root", self._limits.max_page_bytes
        else:
            raise ValueError("html.tree.published-reference-kind-unsupported")
        if reference.size > maximum:
            raise ValueError("html.tree.published-file-bound-exceeded")
        return self._checked(group, name, reference)

    def put_bytes(self, content, *, media_type="application/octet-stream"):
        raise ValueError("html.tree.published-reader-cannot-write")

    def put_file(self, source, *, media_type="application/octet-stream"):
        raise ValueError("html.tree.published-reader-cannot-write")

    def _store_stream(self, reference, reader):
        raise ValueError("html.tree.published-reader-cannot-write")


@contextmanager
def observe_published_html_tree_generation(
    path,
    manifest,
    request,
    *,
    revalidate_parent_paths,
    reserved_memory_bytes=0,
):
    """Reopen all actual generation files and their deterministic commitments.

    The caller independently observes its top-level envelope and complete output
    namespace. This reader never treats their stored assertions as live owners.
    It uses published metadata alone; an absent operator cache cannot manufacture
    staleness and checking creates no directories, temporary files or CAS blobs.
    """
    if os.name != "posix":
        raise ValueError("html.tree.published-generation-platform-unsupported")
    if (
        not isinstance(request, HtmlTreeRenderRequest)
        or not isinstance(manifest, BlobRef)
        or manifest.media_type != "application/json"
        or not 0 < manifest.size <= request.limits.max_index_bytes
        or not callable(revalidate_parent_paths)
    ):
        raise TypeError("html.tree.published-generation-context-required")
    limits = request.limits
    if (
        type(reserved_memory_bytes) is not int
        or not 0 <= reserved_memory_bytes <= limits.max_metadata_memory_bytes
        or reserved_memory_bytes + 32 * limits.max_index_bytes
        > limits.max_metadata_memory_bytes
    ):
        raise ValueError("html.tree.published-generation-memory-bound-exceeded")
    path = _root(Path(path))
    revalidate_parent_paths()
    original_node = _node(path.lstat())
    active = True
    process = os.getpid()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        root_fd = os.open(path, flags)
        stack.callback(os.close, root_fd)
        metadata_fd = os.open("metadata", flags, dir_fd=root_fd)
        stack.callback(os.close, metadata_fd)
        metadata_node = _node(os.fstat(metadata_fd))

        def current():
            if not active or os.getpid() != process:
                raise ValueError("html.tree.published-observation-ended")
            revalidate_parent_paths()
            if (
                _node(_root(path).lstat()) != original_node
                or _node(os.fstat(root_fd)) != original_node
                or _node(os.fstat(metadata_fd)) != metadata_node
                or _node(os.stat("metadata", dir_fd=root_fd, follow_symlinks=False))
                != metadata_node
            ):
                raise ValueError("html.tree.published-generation-custody-changed")

        def read(group, name, maximum):
            current()
            return _read_at(
                metadata_fd if group == "metadata" else root_fd, name, maximum
            )

        store = _PublishedGenerationCAS(path, manifest, read, current, limits)
        try:
            document = _document(store, manifest, limits.max_index_bytes)
            if (
                type(document) is not dict
                or set(document)
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
                or document.get("schema")
                != "literate-ai/retained-tree-html-generation@1"
                or document.get("request_identity") != request.identity.uri
                or type(document.get("page_count")) is not int
                or not 0 < document["page_count"] <= limits.max_pages
            ):
                raise ValueError("html.tree.published-manifest-context-differs")
            source = BlobRef.from_dict(document["source_index"])
            store.set_record_limit(2)
            index = NativeTreeIndex.from_dict(
                _document(store, source, limits.max_index_bytes)
            )
            # Reserve complete reader maps before decoding shards or rendering.
            # These counts are descriptive bounds, not accepted observations;
            # canonical composition and replay verify their actual membership.
            records = (
                3 * index.member_count
                + 3 * len(index.shards)
                + 2 * document["page_count"]
                + 32
            )
            reader_memory = 8192 * records + 32 * limits.max_index_bytes
            if (
                reserved_memory_bytes + reader_memory + index.metadata_memory_bound
                > limits.max_metadata_memory_bytes
            ):
                raise ValueError("html.tree.published-generation-memory-bound-exceeded")
            store.set_record_limit(records)
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
            reserved = reserved_memory_bytes + reader_memory
            for page in emit_html_tree_pages(
                tree, request, reserved_memory_bytes=reserved
            ):
                store.observe_page(page)
            del page
            staged = stage_html_tree_generation(
                tree,
                request,
                _ExistingGenerationCAS(store),
                reserved_memory_bytes=reserved,
            )
            if staged.manifest != manifest:
                raise ValueError("html.tree.published-generation-closure-differs")
            files = stack.enter_context(
                observe_html_tree_generation_files(
                    path,
                    tree,
                    request,
                    store,
                    staged,
                    expected_node=original_node,
                    revalidate_parent_paths=current,
                    reserved_memory_bytes=reserved,
                )
            )
            current()
            yield tree, staged, files
            files.require_current()
            current()
        finally:
            active = False
