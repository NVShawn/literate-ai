"""Read complete physical tree generations without adopting publication rights.

The caller supplies independently held parent custody and the exact directory
node from its live publication transition. This observer checks actual files; it
cannot authorize renaming, index replacement, source exclusion or recovery. A
saved manifest never constructs an output owner.
"""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
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

_ISSUER = object()


class ObservedHtmlTreeGenerationFiles:
    """A finite read-only observation, including one actual owned stage move.

    Rebinding follows the original open directory and exact original file
    signatures. It performs no move and grants no publication or write rights.
    """

    __slots__ = ("_values",)

    def __init__(self, path, guard, moved, *, retained_memory, _issuer):
        if _issuer is not _ISSUER:
            raise TypeError("an actual generation observation context is required")
        object.__setattr__(self, "_values", (path, guard, moved, retained_memory))

    def __setattr__(self, name, value):
        raise TypeError("generation observations are immutable")

    @property
    def path(self):
        self.require_current()
        return self._values[0]()

    def require_current(self, *, reserved_memory_bytes=0):
        self._values[1](reserved_memory_bytes=reserved_memory_bytes)

    def observed_path(self, *, reserved_memory_bytes=0):
        self.require_current(reserved_memory_bytes=reserved_memory_bytes)
        return self._values[0]()

    def rebind_after_owned_move(self, path):
        self._values[2](path)

    @property
    def retained_memory_bytes(self):
        """Descriptive retained map size; no source or publication authority."""
        return self._values[3]()

    def __copy__(self):
        raise TypeError("generation observation cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("generation observation cannot be copied")

    def __reduce__(self):
        raise TypeError("generation observation cannot be restored")


def _node(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid)


def _signature(value):
    return (
        _node(value),
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@contextmanager
def observe_html_tree_generation_files(
    path,
    tree,
    request,
    store,
    staged,
    *,
    expected_node,
    revalidate_parent_paths,
    reserved_memory_bytes=0,
):
    """Yield a context-local complete-file observation, never a publication grant.

    The parent callback checks physical ancestor custody only, not this generation
    recursively. No source payload is read from output: canonical CAS replay
    supplies bounded expected metadata/page bytes. Each subsequent check binds
    the original observed file nodes as well as their exact content. No files,
    CAS members, manifests or parent directories are created or repaired.
    """
    if os.name != "posix":
        raise ValueError("html.tree.native-generation-observation-unsupported")
    if (
        not isinstance(tree, ComposedNativeTree)
        or not isinstance(request, HtmlTreeRenderRequest)
        or not isinstance(store, FileSystemCAS)
        or not isinstance(staged, StagedHtmlTreeGeneration)
        or not callable(revalidate_parent_paths)
    ):
        raise TypeError("html.tree.typed-generation-file-observation-required")
    require_html_tree_generation_receipt(staged)
    limits = request.limits
    if (
        type(reserved_memory_bytes) is not int
        or not 0 <= reserved_memory_bytes <= limits.max_metadata_memory_bytes
    ):
        raise ValueError("html.tree.invalid-memory-reservation")
    # A caller-supplied receipt is descriptive. Validate it against the complete
    # existing CAS before using its totals to bound the physical observation.
    if (
        stage_html_tree_generation(
            tree,
            request,
            _ExistingGenerationCAS(store),
            reserved_memory_bytes=reserved_memory_bytes,
        )
        != staged
    ):
        raise ValueError("html.tree.exact-generation-file-receipt-required")
    path = _root(Path(path))
    revalidate_parent_paths()
    root_node = _node(path.lstat())
    if root_node != expected_node:
        raise ValueError("html.tree.generation-directory-node-differs")
    limits = request.limits
    baseline = (
        tree.index.metadata_memory_bound
        + 16 * staged.metadata_bytes
        + 4096 * (tree.index.member_count + 1)
        + 4 * limits.max_page_bytes
    )
    # Complete canonical replay has independently verified these emitted counts;
    # repeated metadata references can only reduce the physical file namespace.
    maximum_records = staged.page_count + staged.metadata_record_count
    # Both original and next observations coexist during complete replay.
    maps_memory = 8192 * maximum_records
    if (
        baseline + reserved_memory_bytes + maps_memory
        > limits.max_metadata_memory_bytes
    ):
        raise ValueError("html.tree.generation-file-memory-policy-exceeded")
    active = True
    moved = False
    process = os.getpid()
    original = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        root_fd = os.open(path, flags)
        stack.callback(os.close, root_fd)
        if _node(os.fstat(root_fd)) != root_node:
            raise ValueError("html.tree.generation-directory-changed")
        root_signature = _signature(os.fstat(root_fd))
        metadata_fd = os.open("metadata", flags, dir_fd=root_fd)
        stack.callback(os.close, metadata_fd)
        metadata_signature = _signature(os.fstat(metadata_fd))

        def paths_current():
            if not active or os.getpid() != process:
                raise ValueError("html.tree.generation-file-observation-ended")
            revalidate_parent_paths()
            if (
                _signature(_root(path).lstat()) != root_signature
                or _signature(os.fstat(root_fd)) != root_signature
                or _signature(
                    os.stat("metadata", dir_fd=root_fd, follow_symlinks=False)
                )
                != metadata_signature
                or _signature(os.fstat(metadata_fd)) != metadata_signature
            ):
                raise ValueError("html.tree.generation-file-directory-changed")

        def observe(additional_memory_bytes=0):
            if (
                type(additional_memory_bytes) is not int
                or not 0 <= additional_memory_bytes <= limits.max_metadata_memory_bytes
                or baseline
                + reserved_memory_bytes
                + additional_memory_bytes
                + maps_memory
                > limits.max_metadata_memory_bytes
            ):
                raise ValueError("html.tree.generation-file-memory-policy-exceeded")
            paths_current()
            records = {}
            observed_bytes = 0

            def file(group, name, content):
                nonlocal observed_bytes
                paths_current()
                key = group, name
                expected_digest = hashlib.sha256(content).hexdigest()
                if key not in records:
                    if len(records) >= maximum_records:
                        raise ValueError(
                            "html.tree.generation-file-record-policy-exceeded"
                        )
                    if (
                        baseline
                        + reserved_memory_bytes
                        + additional_memory_bytes
                        + 4096
                        * (len(original) if original is not None else len(records) + 1)
                        + 4096 * (len(records) + 1)
                        > limits.max_metadata_memory_bytes
                    ):
                        raise ValueError(
                            "html.tree.generation-file-memory-policy-exceeded"
                        )
                    observed_bytes += len(content)
                    if observed_bytes > limits.max_retained_generation_bytes:
                        raise ValueError(
                            "html.tree.generation-file-storage-policy-exceeded"
                        )
                directory = metadata_fd if group == "metadata" else root_fd
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    before = os.fstat(fd)
                    signature = _signature(before)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or before.st_size != len(content)
                    ):
                        raise ValueError("html.tree.generation-file-unsafe")
                    record = (signature, expected_digest, len(content))
                    if (key in records and records[key] != record) or (
                        original is not None and original.get(key) != record
                    ):
                        raise ValueError("html.tree.generation-file-node-changed")
                    hashed = hashlib.sha256()
                    count = 0
                    while count <= len(content):
                        block = os.read(fd, min(65536, len(content) - count + 1))
                        if not block:
                            break
                        count += len(block)
                        hashed.update(block)
                    if (
                        count != len(content)
                        or hashed.hexdigest() != expected_digest
                        or _signature(os.fstat(fd)) != signature
                        or _signature(
                            os.stat(name, dir_fd=directory, follow_symlinks=False)
                        )
                        != signature
                    ):
                        raise ValueError("html.tree.generation-file-bytes-changed")
                    records[key] = record
                finally:
                    os.close(fd)
                paths_current()

            class PhysicalCAS(_ExistingGenerationCAS):
                def put_bytes(self, content, *, media_type="application/octet-stream"):
                    reference = super().put_bytes(content, media_type=media_type)
                    if media_type == "application/json":
                        if reference == staged.manifest:
                            file("root", "manifest.json", content)
                        else:
                            file("metadata", reference.digest + ".json", content)
                    return reference

            for page in emit_html_tree_pages(
                tree,
                request,
                reserved_memory_bytes=(
                    reserved_memory_bytes + additional_memory_bytes + maps_memory
                ),
            ):
                file("root", page.path, page.content)
            del page
            if (
                stage_html_tree_generation(
                    tree,
                    request,
                    PhysicalCAS(store),
                    reserved_memory_bytes=(
                        reserved_memory_bytes + additional_memory_bytes + maps_memory
                    ),
                )
                != staged
            ):
                raise ValueError("html.tree.physical-generation-receipt-mismatch")
            for group, directory, extras in (
                ("root", root_fd, {"metadata"}),
                ("metadata", metadata_fd, set()),
            ):
                expected = {name for kind, name in records if kind == group} | extras
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.name not in expected:
                            raise ValueError("html.tree.foreign-generation-file")
                        expected.remove(entry.name)
                if expected:
                    raise ValueError("html.tree.generation-file-missing")
            # Replaying later descriptors must not hide an edit to an earlier
            # page. Join every original opened-file signature to its current
            # pathname after the complete replay and namespace enumeration.
            for (group, name), (signature, _digest, _size) in records.items():
                directory = metadata_fd if group == "metadata" else root_fd
                if (
                    _signature(os.stat(name, dir_fd=directory, follow_symlinks=False))
                    != signature
                ):
                    raise ValueError(
                        "html.tree.generation-file-changed-during-observation"
                    )
            paths_current()
            if original is not None and records != original:
                raise ValueError("html.tree.generation-file-membership-changed")
            return records

        original = observe()

        def require_current(*, reserved_memory_bytes=0):
            observe(reserved_memory_bytes)

        def rebind_after_owned_move(destination):
            nonlocal path, root_signature, active, moved
            if not active or os.getpid() != process or moved:
                raise ValueError("html.tree.generation-move-observation-unavailable")
            destination = Path(destination)
            # This one transition tracks payload -> its enclosing namespace's
            # immutable generation. Arbitrary relocation is not an interface.
            if (
                path.name != "payload"
                or not path.parent.name.startswith(".staging-")
                or not destination.is_absolute()
                or destination.parent != path.parent.parent
                or destination.name != "g-" + staged.manifest.digest
                or os.path.lexists(path)
            ):
                raise ValueError("html.tree.generation-move-coordinate-differs")
            revalidate_parent_paths()
            destination = _root(destination)
            after = _signature(destination.lstat())
            # Native rename may change directory ctime. Names/content topology,
            # mode, uid/gid, inode, size and mtime must remain the original facts;
            # every child is independently joined to its full original signature.
            if (
                after[:-1] != root_signature[:-1]
                or _signature(os.fstat(root_fd)) != after
            ):
                raise ValueError("html.tree.generation-move-node-differs")
            path = destination
            root_signature = after
            moved = True
            try:
                observe()
            except BaseException:
                active = False
                raise

        def retained_memory():
            if not active or os.getpid() != process:
                raise ValueError("html.tree.generation-file-observation-ended")
            return 4096 * len(original)

        try:
            yield ObservedHtmlTreeGenerationFiles(
                lambda: path,
                require_current,
                rebind_after_owned_move,
                retained_memory=retained_memory,
                _issuer=_ISSUER,
            )
        finally:
            active = False
