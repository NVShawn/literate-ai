"""Provisional physical files for a complete tree generation, never an index.

The caller must hold a separately authorized output namespace and supply its
currentness guard. This module exclusively creates one private staging directory
there; it neither adopts existing output nor publishes/collects generations.
Interruption preserves identifiable provisional files for explicit recovery.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.native_tree_shards import ComposedNativeTree
from literate_ai.storage import FileSystemCAS

from .html_tree_emitter import emit_html_tree_pages
from .html_tree_generation import (
    StagedHtmlTreeGeneration,
    require_html_tree_generation_receipt,
    stage_html_tree_generation,
)
from .html_tree_generation_check import _ExistingGenerationCAS
from .retained_project_inputs import _root

_STAGE_ISSUER = object()


def _node(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
    )


def _signature(metadata):
    return (
        _node(metadata),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class ProvisionalHtmlTreeFiles:
    """Actual context-local file custody, not decoded publication permission."""

    __slots__ = ("_values",)

    def __init__(self, path, wrapper, guard, *, retained_memory, _issuer):
        if _issuer is not _STAGE_ISSUER:
            raise TypeError("an actual newly created staging context is required")
        object.__setattr__(self, "_values", (path, wrapper, guard, retained_memory))

    def __setattr__(self, name, value):
        raise TypeError("provisional output observations are immutable")

    @property
    def payload_path(self):
        self.require_current()
        return self._values[0]

    @property
    def wrapper_path(self):
        self.require_current()
        return self._values[1]

    def require_current(self):
        self._values[2]()

    @property
    def retained_memory_bytes(self):
        """Descriptive live allocation estimate; never a filesystem grant."""
        return self._values[3]()

    def __copy__(self):
        raise TypeError("provisional output custody cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("provisional output custody cannot be copied")

    def __reduce__(self):
        raise TypeError("provisional output custody cannot be restored")


@contextmanager
def stage_html_tree_files(
    parent, tree, request, store, staged, *, revalidate_output, reserved_memory_bytes=0
):
    """Write and reopen every provisional page/descriptor under an actual owner.

    A returned object is usable only inside this context. The caller still owns
    input freshness, complete namespace/storage reservation, immutable generation
    publication and the atomic top-level index commit. Its guard receives None
    before creation and the actual live provisional object after complete staging,
    so it can verify that new member alongside the existing namespace. Per-file
    work uses held directory custody; whole closure checks run at boundaries,
    avoiding quadratic rescanning. No cleanup is implied.
    """
    if os.name != "posix":
        raise ValueError("html.tree.native-file-staging-unsupported")
    if (
        not isinstance(tree, ComposedNativeTree)
        or not isinstance(request, HtmlTreeRenderRequest)
        or not isinstance(store, FileSystemCAS)
        or not isinstance(staged, StagedHtmlTreeGeneration)
        or not callable(revalidate_output)
    ):
        raise TypeError("html.tree.typed-owned-file-staging-required")
    require_html_tree_generation_receipt(staged)
    limits = request.limits
    if (
        type(reserved_memory_bytes) is not int
        or not 0 <= reserved_memory_bytes <= limits.max_metadata_memory_bytes
    ):
        raise ValueError("html.tree.invalid-memory-reservation")
    # The public receipt is descriptive data. Verify its complete deterministic
    # CAS closure before trusting its sizes for allocation or creating any files.
    if (
        stage_html_tree_generation(
            tree,
            request,
            _ExistingGenerationCAS(store.root),
            reserved_memory_bytes=reserved_memory_bytes,
        )
        != staged
    ):
        raise ValueError("html.tree.exact-staged-generation-required")
    parent = _root(Path(parent))
    revalidate_output(None)
    initial = parent.lstat()
    if initial.st_uid != os.geteuid() or stat.S_IMODE(initial.st_mode) & 0o077:
        raise ValueError("html.tree.private-owned-output-namespace-required")
    limits = request.limits
    baseline = (
        tree.index.metadata_memory_bound
        + 16 * staged.metadata_bytes
        + 4096 * (tree.index.member_count + 1)
        + 4 * limits.max_page_bytes
    )
    # Complete comparison-only canonical replay above validates this descriptive
    # record count before it controls allocation. Repeated references may reduce
    # physical files; count all emitted records conservatively, plus our control.
    maximum_records = staged.page_count + staged.metadata_record_count + 1
    map_memory = 4096 * maximum_records
    if baseline + reserved_memory_bytes + map_memory > limits.max_metadata_memory_bytes:
        raise ValueError("html.tree.file-staging-memory-policy-exceeded")
    active = True
    process = os.getpid()
    records = {}
    total_bytes = 0
    nonce = secrets.token_hex(16)
    name = ".staging-" + nonce
    path = parent / name
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        parent_fd = os.open(parent, flags)
        stack.callback(os.close, parent_fd)
        if _node(os.fstat(parent_fd)) != _node(initial):
            raise ValueError("html.tree.output-parent-changed")
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        root_fd = os.open(name, flags, dir_fd=parent_fd)
        stack.callback(os.close, root_fd)
        root_node = _node(os.fstat(root_fd))
        os.mkdir("payload", 0o700, dir_fd=root_fd)
        payload_fd = os.open("payload", flags, dir_fd=root_fd)
        stack.callback(os.close, payload_fd)
        payload_node = _node(os.fstat(payload_fd))
        os.mkdir("metadata", 0o700, dir_fd=payload_fd)
        metadata_fd = os.open("metadata", flags, dir_fd=payload_fd)
        stack.callback(os.close, metadata_fd)
        metadata_node = _node(os.fstat(metadata_fd))

        def paths_current():
            if not active or os.getpid() != process:
                raise ValueError("html.tree.provisional-file-context-ended")
            if (
                _node(_root(parent).lstat()) != _node(initial)
                or _node(os.fstat(parent_fd)) != _node(initial)
                or _node(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
                != root_node
                or _node(os.fstat(root_fd)) != root_node
                or _node(os.stat("payload", dir_fd=root_fd, follow_symlinks=False))
                != payload_node
                or _node(os.fstat(payload_fd)) != payload_node
                or _node(os.stat("metadata", dir_fd=payload_fd, follow_symlinks=False))
                != metadata_node
                or _node(os.fstat(metadata_fd)) != metadata_node
            ):
                raise ValueError("html.tree.provisional-directory-custody-changed")

        def reopen(directory, filename, expected, digest, size):
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or _node(before) != expected
                    or before.st_size != size
                ):
                    raise ValueError("html.tree.provisional-file-custody-changed")
                hashed = hashlib.sha256()
                observed = 0
                while observed <= size:
                    block = os.read(descriptor, min(65536, size - observed + 1))
                    if not block:
                        break
                    observed += len(block)
                    hashed.update(block)
                if (
                    observed != size
                    or hashed.hexdigest() != digest
                    or _signature(os.fstat(descriptor)) != _signature(before)
                    or _signature(
                        os.stat(filename, dir_fd=directory, follow_symlinks=False)
                    )
                    != _signature(before)
                ):
                    raise ValueError("html.tree.provisional-file-bytes-changed")
            finally:
                os.close(descriptor)

        def write(group, filename, content):
            nonlocal total_bytes
            paths_current()
            directory = {
                "control": root_fd,
                "payload": payload_fd,
                "metadata": metadata_fd,
            }[group]
            key = group, filename
            digest = hashlib.sha256(content).hexdigest()
            if key in records:
                node, known_digest, size = records[key]
                if (known_digest, size) != (digest, len(content)):
                    raise ValueError("html.tree.provisional-name-collision")
                reopen(directory, filename, node, digest, size)
                return
            if len(records) >= maximum_records or (
                baseline + reserved_memory_bytes + 4096 * (len(records) + 1)
                > limits.max_metadata_memory_bytes
            ):
                raise ValueError("html.tree.file-staging-memory-policy-exceeded")
            if total_bytes + len(content) > limits.max_retained_generation_bytes:
                raise ValueError("html.tree.file-staging-storage-policy-exceeded")
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                node = _node(os.fstat(descriptor))
                remaining = memoryview(content)
                while remaining:
                    count = os.write(descriptor, remaining)
                    if count <= 0:
                        raise ValueError("html.tree.provisional-write-failed")
                    remaining = remaining[count:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            records[key] = node, digest, len(content)
            total_bytes += len(content)
            reopen(directory, filename, node, digest, len(content))
            paths_current()

        write(
            "control",
            "owner.json",
            canonical_json_bytes(
                {
                    "schema": "literate-ai/provisional-tree-html-files@1",
                    "request_identity": request.identity.uri,
                    "manifest": staged.manifest.to_dict(),
                    "nonce": nonce,
                    "source_currentness_claimed": False,
                    "published": False,
                }
            ),
        )

        class MaterializingCAS(_ExistingGenerationCAS):
            def put_bytes(self, content, *, media_type="application/octet-stream"):
                reference = super().put_bytes(content, media_type=media_type)
                if media_type == "application/json":
                    if reference == staged.manifest:
                        write("payload", "manifest.json", content)
                    else:
                        write("metadata", reference.digest + ".json", content)
                return reference

        try:
            for page in emit_html_tree_pages(
                tree, request, reserved_memory_bytes=reserved_memory_bytes + map_memory
            ):
                write("payload", page.path, page.content)
            del page
            checked = stage_html_tree_generation(
                tree,
                request,
                MaterializingCAS(store.root),
                reserved_memory_bytes=reserved_memory_bytes + map_memory,
            )
            if checked != staged:
                raise ValueError("html.tree.physical-generation-manifest-mismatch")

            def require_complete():
                paths_current()
                for group, directory, extra in (
                    ("control", root_fd, {"payload"}),
                    ("payload", payload_fd, {"metadata"}),
                    ("metadata", metadata_fd, set()),
                ):
                    expected = {
                        filename for kind, filename in records if kind == group
                    } | extra
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if entry.name not in expected:
                                raise ValueError("html.tree.foreign-provisional-member")
                            expected.remove(entry.name)
                    if expected:
                        raise ValueError("html.tree.missing-provisional-member")
                for (group, filename), (node, digest, size) in records.items():
                    directory = {
                        "control": root_fd,
                        "payload": payload_fd,
                        "metadata": metadata_fd,
                    }[group]
                    reopen(directory, filename, node, digest, size)
                paths_current()

            require_complete()
            for descriptor in (metadata_fd, payload_fd, root_fd, parent_fd):
                os.fsync(descriptor)

            def retained_memory():
                if not active or os.getpid() != process:
                    raise ValueError("html.tree.provisional-file-context-ended")
                return 4096 * len(records)

            provisional = ProvisionalHtmlTreeFiles(
                path / "payload",
                path,
                require_complete,
                retained_memory=retained_memory,
                _issuer=_STAGE_ISSUER,
            )
            revalidate_output(provisional)
            require_complete()
            yield provisional
        finally:
            active = False
