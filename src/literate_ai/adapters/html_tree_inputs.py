"""Actual source observations through canonical complete namespace capture.

Saved descriptors cannot create this context. Inside-source publication uses the
original source view tied to its independently issued output owner, not an ignore
list or a caller-supplied currentness assertion.
"""

from __future__ import annotations

import os
import weakref
from contextlib import contextmanager
from pathlib import Path

from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.storage import FileSystemCAS

from .native_sdk_tree_custody import _capture_composed_native_tree
from .retained_project_inputs import _root

_SOURCE_ISSUER = object()
_SOURCE_OBSERVATIONS = weakref.WeakKeyDictionary()


def _source_record(view):
    if type(view) is not HtmlTreeSourceObservation:
        raise TypeError("html.tree.original-source-observation-required")
    record = _SOURCE_OBSERVATIONS.get(view)
    if record is None:
        raise ValueError("html.tree.source-observation-not-issued-or-ended")
    record["paths_current"]()
    return record


class HtmlTreeSourceObservation:
    """One original finite source observation, never a publication capability."""

    __slots__ = ("__weakref__",)

    def __init__(self, *, _issuer):
        if _issuer is not _SOURCE_ISSUER:
            raise TypeError("html.tree.original-source-observation-required")

    @property
    def tree(self):
        """Captured bytes and geometry; require_current independently rehashes."""
        return _source_record(self)["tree"]

    @property
    def retained_memory_bytes(self):
        return self.tree.index.metadata_memory_bound

    def require_current(self, *, reserved_memory_bytes=0):
        _source_record(self)["require_current"](reserved_memory_bytes)

    def __copy__(self):
        raise TypeError("source observations cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("source observations cannot be copied")

    def __reduce__(self):
        raise TypeError("source observations cannot be restored")


def _require_html_tree_source_observation(
    view, project, request, output_owner, *, expected_tree
):
    record = _source_record(view)
    if (
        record["project"] != _root(Path(project))
        or record["request"].identity != request.identity
        or record["output_owner"] is not output_owner
        or record["tree"] is not expected_tree
        or record["tree"].identity != expected_tree.identity
    ):
        raise ValueError("html.tree.source-output-observation-mismatch")
    return view.require_current


@contextmanager
def observe_html_tree_inputs(project_root, request, store):
    """Preserve the existing external-output observation pair interface."""
    with _observe_html_tree_inputs(project_root, request, store, None) as observed:
        yield observed.tree, observed.require_current


@contextmanager
def observe_owned_html_tree_inputs(project_root, request, store, *, output_owner):
    """Observe complete inputs excluding only a separate original output owner.

    The source, CAS and selected output remain independently guarded. This view
    neither writes output nor adopts a namespace from historical metadata.
    """
    from .html_tree_output_initialization import _html_tree_capture_exclusions

    if not isinstance(request, HtmlTreeRenderRequest):
        raise TypeError("html.tree.typed-source-observation-context-required")
    project = _root(Path(project_root))
    _html_tree_capture_exclusions(output_owner, _root(project / request.source_root))
    with _observe_html_tree_inputs(project, request, store, output_owner) as observed:
        yield observed


@contextmanager
def _observe_html_tree_inputs(project_root, request, store, output_owner):
    if not isinstance(request, HtmlTreeRenderRequest) or not isinstance(
        store, FileSystemCAS
    ):
        raise TypeError("html.tree.typed-source-observation-context-required")
    project = _root(Path(project_root))
    source = _root(project / request.source_root)
    if not source.is_relative_to(project):
        raise ValueError("html.tree.source-outside-selected-project")
    limits = request.limits
    receiver_pid = os.getpid()
    active = True
    observed = None
    issued_view = None

    def identity(path):
        node = _root(path).lstat()
        return node.st_dev, node.st_ino, node.st_mode, node.st_uid, node.st_gid

    project_identity = identity(project)
    source_identity = identity(source)
    store_identity = identity(store.root)

    def require_paths():
        if not active or os.getpid() != receiver_pid:
            raise ValueError("html.tree.source-observation-ended")
        if (
            identity(project) != project_identity
            or identity(source) != source_identity
            or identity(store.root) != store_identity
        ):
            raise ValueError("html.tree.source-observation-custody-changed")

    def capture(additional_memory_bytes=0):
        require_paths()
        if (
            type(additional_memory_bytes) is not int
            or not 0 <= additional_memory_bytes <= limits.max_metadata_memory_bytes
        ):
            raise ValueError("html.tree.invalid-memory-reservation")
        retained = (
            additional_memory_bytes
            + (0 if observed is None else observed.index.metadata_memory_bound)
            + (0 if output_owner is None else output_owner.retained_memory_bytes)
        )
        if retained >= limits.max_metadata_memory_bytes:
            raise ValueError("html.tree.source-observation-memory-policy-exceeded")
        tree = _capture_composed_native_tree(
            source,
            store=store,
            limits=limits.source,
            case_sensitive=request.case_sensitive,
            max_descriptor_bytes=limits.max_descriptor_bytes,
            max_descriptor_total_bytes=limits.max_descriptor_total_bytes,
            max_index_bytes=limits.max_index_bytes,
            max_metadata_memory_bytes=limits.max_metadata_memory_bytes,
            output_custody=output_owner,
            reserved_memory_bytes=retained,
            budget_capture=True,
        )
        require_paths()
        return tree

    def require_current(additional_memory_bytes=0):
        if capture(additional_memory_bytes).identity != observed.identity:
            raise ValueError("html.tree.source-observation-stale")
        require_paths()

    try:
        observed = capture()
        issued_view = HtmlTreeSourceObservation(_issuer=_SOURCE_ISSUER)
        _SOURCE_OBSERVATIONS[issued_view] = {
            "project": project,
            "request": request,
            "output_owner": output_owner,
            "tree": observed,
            "paths_current": require_paths,
            "require_current": require_current,
        }
        yield issued_view
    finally:
        active = False
        if issued_view is not None:
            _SOURCE_OBSERVATIONS.pop(issued_view, None)
