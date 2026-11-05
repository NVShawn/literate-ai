"""Finite observations of complete published output; no publication capability.

An actual current descriptor context, complete generation and index observation
can select the exact generated paths during a separate source observation. Saved
assertions alone cannot create this context or select exclusions. No method
creates, repairs, locks, replaces or collects any output.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import weakref
from contextlib import ExitStack, contextmanager
from pathlib import Path

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes

from .html_tree_output_initialization import (
    MAX_INITIAL_TREE_INDEX_BYTES,
    _initial_index,
    _node,
    _read_at,
    _signature,
)
from .html_tree_published_generation import observe_published_html_tree_generation
from .retained_project_inputs import _root

_ISSUER = object()
_OBSERVATIONS = weakref.WeakKeyDictionary()


def _record(observation):
    if type(observation) is not ReadOnlyHtmlTreeOutput:
        raise TypeError("html.tree.original-readonly-output-required")
    record = _OBSERVATIONS.get(observation)
    if record is None:
        raise ValueError("html.tree.readonly-output-observation-ended")
    record["paths_current"]()
    return record


class ReadOnlyHtmlTreeOutput:
    """Original context-local observation, with no write or recovery methods."""

    __slots__ = ("__weakref__",)

    def __init__(self, *, _issuer):
        if _issuer is not _ISSUER:
            raise TypeError("html.tree.original-readonly-output-required")

    @property
    def tree(self):
        return _record(self)["tree"]

    @property
    def staged(self):
        return _record(self)["staged"]

    @property
    def retained_memory_bytes(self):
        return _record(self)["retained_memory"]

    def require_current(self, *, reserved_memory_bytes=0):
        _record(self)["current"](reserved_memory_bytes=reserved_memory_bytes)

    def __copy__(self):
        raise TypeError("readonly output observations cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("readonly output observations cannot be copied")

    def __reduce__(self):
        raise TypeError("readonly output observations cannot be restored")


def _readonly_html_tree_capture_exclusions(
    observation, source, *, reserved_memory_bytes=0
):
    record = _record(observation)

    def require_capture_current(*, reserved_memory_bytes=0):
        # Canonical capture reports its GLOBAL reservation, which already
        # includes this original output observation. The public observation
        # method instead receives ADDITIONAL memory. Subtract only our exact
        # retained charge; never clamp a missing/invalid reservation to zero.
        retained = record["retained_memory"]
        if type(reserved_memory_bytes) is not int or reserved_memory_bytes < retained:
            raise ValueError("html.tree.readonly-output-reservation-missing")
        observation.require_current(
            reserved_memory_bytes=reserved_memory_bytes - retained
        )

    require_capture_current(reserved_memory_bytes=reserved_memory_bytes)
    source = _root(Path(source))
    if source != record["source"]:
        raise ValueError("html.tree.readonly-output-source-selection-differs")
    namespace, index = record["namespace"], record["index"]
    directories = frozenset(
        (namespace.relative_to(source).as_posix(),)
        if namespace.is_relative_to(source)
        else ()
    )
    files = frozenset(
        (index.relative_to(source).as_posix(),) if index.is_relative_to(source) else ()
    )
    return directories, files, require_capture_current


def _control(content, schema, fields):
    document = json.loads(content)
    if (
        type(document) is not dict
        or set(document) != fields
        or document.get("schema") != schema
        or canonical_json_bytes(document) != content
    ):
        raise ValueError("html.tree.published-control-unrecognized")
    return document


@contextmanager
def observe_published_html_tree_output(project_root, request):
    """Reobserve an exact existing initial-format artifact without adopting it.

    Recognition proves data closure only. In particular, a copied owner.json
    cannot confer a publisher, a recovered reservation or permission to replace
    an index. The separate current source capture must still agree with the
    historical source commitment before freshness can be reported.
    """
    if os.name != "posix":
        raise ValueError("html.tree.readonly-output-platform-unsupported")
    if not isinstance(request, HtmlTreeRenderRequest):
        raise TypeError("html.tree.typed-readonly-output-request-required")
    # Admit bounded control bookkeeping before opening/collecting output data.
    # Per-control decoded bytes are charged from actual native sizes below.
    controls_memory = 65536
    if controls_memory > request.limits.max_metadata_memory_bytes:
        raise ValueError("html.tree.published-output-memory-bound-exceeded")

    def read_control(directory, name, maximum):
        nonlocal controls_memory
        node = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(node.st_mode)
            or node.st_nlink != 1
            or not 0 < node.st_size <= maximum
        ):
            raise ValueError("html.tree.bounded-published-control-required")
        charge = 16 * node.st_size
        if controls_memory + charge > request.limits.max_metadata_memory_bytes:
            raise ValueError("html.tree.published-output-memory-bound-exceeded")
        controls_memory += charge
        # A growing/replaced control cannot consume a larger allocation between
        # stat and read: the canonical no-follow reader receives this exact cap.
        snapshot = _read_at(directory, name, node.st_size)
        if snapshot[0] != _signature(node):
            raise ValueError("html.tree.published-output-controls-changed")
        return snapshot

    project = _root(Path(project_root))
    source = _root(project / request.source_root)
    output = _root(project / request.output_root)
    if not source.is_relative_to(project) or not output.is_relative_to(project):
        raise ValueError("html.tree.readonly-output-outside-project")
    namespace = output / (".litai-tree-" + request.request_id)
    index = output / "index.html"
    active = True
    process = os.getpid()
    issued = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        output_fd = os.open(output, flags)
        stack.callback(os.close, output_fd)
        namespace_fd = os.open(namespace.name, flags, dir_fd=output_fd)
        stack.callback(os.close, namespace_fd)
        nodes = {
            project: _node(project.lstat()),
            source: _node(source.lstat()),
            output: _node(output.lstat()),
        }
        namespace_signature = _signature(os.fstat(namespace_fd))
        index_snapshot = read_control(
            output_fd,
            "index.html",
            min(MAX_INITIAL_TREE_INDEX_BYTES, request.limits.max_page_bytes),
        )
        owner_snapshot = read_control(namespace_fd, "owner.json", 4096)
        owner = _control(
            owner_snapshot[1],
            "literate-ai/initial-tree-html-output-assertion@1",
            {
                "schema",
                "request_identity",
                "plan_identity",
                "nonce",
                "published",
                "recovery_is_not_live_ownership",
            },
        )
        ContentIdentity.parse_uri(owner["plan_identity"])
        if (
            owner["request_identity"] != request.identity.uri
            or type(owner["nonce"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", owner["nonce"]) is None
            or owner["published"] is not False
            or owner["recovery_is_not_live_ownership"] is not True
        ):
            raise ValueError("html.tree.published-control-context-differs")
        manifests = re.findall(
            rb'<meta name="litai-tree-manifest" content="sha256:([0-9a-f]{64})">',
            index_snapshot[1],
        )
        if len(manifests) != 1:
            raise ValueError("html.tree.published-index-unrecognized")
        digest = manifests[0].decode("ascii")
        generation = namespace / ("g-" + digest)
        with os.scandir(namespace_fd) as entries:
            names = set()
            for entry in entries:
                if len(names) >= 3:
                    raise ValueError("html.tree.foreign-published-namespace-member")
                names.add(entry.name)
        wrappers = [
            name for name in names if re.fullmatch(r"\.staging-[0-9a-f]{32}", name)
        ]
        if len(wrappers) != 1 or names != {"owner.json", generation.name, wrappers[0]}:
            raise ValueError("html.tree.published-namespace-incomplete")
        wrapper_name = wrappers[0]
        wrapper_fd = os.open(wrapper_name, flags, dir_fd=namespace_fd)
        stack.callback(os.close, wrapper_fd)
        wrapper_signature = _signature(os.fstat(wrapper_fd))
        wrapper_snapshot = read_control(wrapper_fd, "owner.json", 4096)
        provisional = _control(
            wrapper_snapshot[1],
            "literate-ai/provisional-tree-html-files@1",
            {
                "schema",
                "request_identity",
                "manifest",
                "nonce",
                "source_currentness_claimed",
                "published",
            },
        )
        manifest = BlobRef.from_dict(provisional["manifest"])
        if (
            manifest.digest != digest
            or manifest.media_type != "application/json"
            or provisional["request_identity"] != request.identity.uri
            or provisional["nonce"] != wrapper_name.removeprefix(".staging-")
            or provisional["source_currentness_claimed"] is not False
            or provisional["published"] is not False
        ):
            raise ValueError("html.tree.published-stage-context-differs")

        def paths_current():
            if not active or os.getpid() != process:
                raise ValueError("html.tree.readonly-output-observation-ended")
            if (
                any(_node(_root(path).lstat()) != node for path, node in nodes.items())
                or _node(os.fstat(output_fd)) != nodes[output]
                or _signature(os.fstat(namespace_fd)) != namespace_signature
                or _signature(
                    os.stat(namespace.name, dir_fd=output_fd, follow_symlinks=False)
                )
                != namespace_signature
                or _signature(os.fstat(wrapper_fd)) != wrapper_signature
                or _signature(
                    os.stat(wrapper_name, dir_fd=namespace_fd, follow_symlinks=False)
                )
                != wrapper_signature
                or _signature(
                    os.stat("index.html", dir_fd=output_fd, follow_symlinks=False)
                )
                != index_snapshot[0]
            ):
                raise ValueError("html.tree.published-output-custody-changed")

        def controls_current():
            paths_current()
            if (
                _read_at(output_fd, "index.html", len(index_snapshot[1]))
                != index_snapshot
                or _read_at(namespace_fd, "owner.json", 4096) != owner_snapshot
                or _read_at(wrapper_fd, "owner.json", 4096) != wrapper_snapshot
            ):
                raise ValueError("html.tree.published-output-controls-changed")
            for descriptor, expected in (
                (namespace_fd, names),
                (wrapper_fd, {"owner.json"}),
            ):
                with os.scandir(descriptor) as entries:
                    remaining = set(expected)
                    for entry in entries:
                        if entry.name not in remaining:
                            raise ValueError(
                                "html.tree.foreign-published-namespace-member"
                            )
                        remaining.remove(entry.name)
                    if remaining:
                        raise ValueError("html.tree.published-namespace-incomplete")
            paths_current()

        try:
            controls_current()
            tree, staged, files = stack.enter_context(
                observe_published_html_tree_generation(
                    generation,
                    manifest,
                    request,
                    revalidate_parent_paths=paths_current,
                    reserved_memory_bytes=controls_memory,
                )
            )
            if _initial_index(request, staged) != index_snapshot[1]:
                raise ValueError("html.tree.published-index-context-differs")
            retained = (
                controls_memory
                + tree.index.metadata_memory_bound
                + 8192
                * (
                    3 * tree.index.member_count
                    + 3 * len(tree.index.shards)
                    + 2 * staged.page_count
                    + 32
                )
                + 32 * request.limits.max_index_bytes
                + files.retained_memory_bytes
            )
            if (
                staged.page_bytes
                + staged.metadata_bytes
                + sum(
                    len(value[1])
                    for value in (index_snapshot, owner_snapshot, wrapper_snapshot)
                )
                > request.limits.max_retained_generation_bytes
            ):
                raise ValueError("html.tree.published-output-storage-bound-exceeded")

            def require_current(*, reserved_memory_bytes=0):
                if (
                    type(reserved_memory_bytes) is not int
                    or reserved_memory_bytes < 0
                    or retained + reserved_memory_bytes
                    > request.limits.max_metadata_memory_bytes
                ):
                    raise ValueError("html.tree.published-output-memory-bound-exceeded")
                controls_current()
                files.require_current(reserved_memory_bytes=reserved_memory_bytes)
                controls_current()

            require_current()
            issued = ReadOnlyHtmlTreeOutput(_issuer=_ISSUER)
            _OBSERVATIONS[issued] = {
                "paths_current": paths_current,
                "current": require_current,
                "source": source,
                "namespace": namespace,
                "index": index,
                "tree": tree,
                "staged": staged,
                "retained_memory": retained,
            }
            yield issued
            require_current()
        finally:
            try:
                # Nested generation closing guards still need these original
                # parent descriptors and their live read-only context.
                # Forward an existing refusal to nested contexts. Their normal
                # closing replays must not run as if a failed capture succeeded.
                stack.__exit__(*sys.exc_info())
            finally:
                active = False
                if issued is not None:
                    _OBSERVATIONS.pop(issued, None)
