"""Initial tree HTML publication through current source and output owners.

This additive boundary creates a fresh generation only after acknowledgement of
an actual current read-only plan. It never reopens historical ownership, retries
an interrupted namespace, replaces an existing index, or collects old evidence.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from literate_ai._cache_lock import cache_lock_path, exclusive_cache_lock
from literate_ai._filesystem import ensure_safe_directory, require_safe_directory
from literate_ai.cache_directories import (
    ensure_cache_directory,
    require_cache_directory_current,
)
from literate_ai.contracts.html_trees import HtmlTreeViewDeclaration
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.storage import FileSystemCAS

from .html_publication import read_output
from .html_tree_generation import stage_html_tree_generation
from .html_tree_inputs import observe_owned_html_tree_inputs
from .html_tree_output_initialization import initialize_html_tree_output
from .html_tree_render import _framework, _renderer_identity, plan_html_tree_render
from .repository_refresh import _local_identity_wire
from .retained_project_inputs import _root


def _node(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
    )


def _private_directory(path):
    require_safe_directory(path)
    metadata = path.lstat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("html.tree.private-state-directory-required")
    return _node(metadata)


def _disjoint(left, right):
    return not (left.is_relative_to(right) or right.is_relative_to(left))


def apply_initial_html_tree(
    root,
    declaration,
    *,
    acknowledged_plan_identity,
    state_root,
    require_declaration_current,
):
    """Apply one fresh full-plan acknowledgement, returning descriptive facts.

    ``state_root`` must be outside both source and output. A missing canonical
    operator state root is created privately only after fresh acknowledgement;
    existing broad permissions are never changed. A plan cannot select state or
    restore an output capability. The caller's declaration guard keeps the actual
    authored input current, independently of complete source recapture and the
    separately issued output owner.
    """
    if os.name != "posix":
        raise ValueError("html.tree.native-initial-apply-unsupported")
    if (
        not isinstance(declaration, HtmlTreeViewDeclaration)
        or not isinstance(acknowledged_plan_identity, ContentIdentity)
        or not callable(require_declaration_current)
    ):
        raise TypeError("html.tree.typed-initial-apply-required")
    require_declaration_current()
    root = _root(Path(root))
    plan = plan_html_tree_render(root, declaration)
    require_declaration_current()
    if canonical_identity(plan) != acknowledged_plan_identity:
        raise ValueError("html.tree.full-plan-acknowledgement-mismatch")
    renderer = ContentIdentity.from_dict(plan["renderer_identity"])
    request = declaration.resolve(renderer)
    source = _root(root / request.source_root)
    output = _root(root / request.output_root)
    # No creation, including lock ancestors, precedes these physical checks.
    state = Path(state_root).absolute()
    if state != state.resolve(strict=False):
        raise ValueError("html.tree.state-path-redirected")
    require_safe_directory(state, allow_missing=True)
    if not _disjoint(state, source) or not _disjoint(state, output):
        raise ValueError("html.tree.state-overlaps-source-or-output")
    if os.path.lexists(state):
        _private_directory(state)
    else:
        require_declaration_current()
        if plan_html_tree_render(root, declaration) != plan:
            raise ValueError("html.tree.initial-plan-changed")
        require_declaration_current()
        require_safe_directory(state, allow_missing=True)
        ensure_safe_directory(state, mode=0o700)
    state_node = _private_directory(state)
    target_identity = canonical_identity(
        {
            "schema": "literate-ai/tree-html-publication-target@1",
            "project_node": plan["output_plan"]["project_node"],
            "index": str(root / request.index_path),
        }
    )
    managed = state / "tree-html"
    target = managed / target_identity.digest
    cas_root = target / "cas"
    # The same physical target shares a lock even across different request IDs.
    # Its owner root is external state, never the observed source/project root.
    lock = cache_lock_path(
        state, root / request.index_path, target_identity.digest, "publication.lock"
    )
    lock_descriptor = None
    lock_node = None

    def locked(descriptor):
        nonlocal lock_descriptor, lock_node
        lock_descriptor = descriptor
        lock_node = _node(os.fstat(descriptor))

    with exclusive_cache_lock(lock, timeout_seconds=0, on_locked=locked):
        managed_nodes = {}

        def require_paths():
            require_declaration_current()
            if _private_directory(state) != state_node:
                raise ValueError("html.tree.state-custody-changed")
            require_safe_directory(lock.parent)
            held = os.fstat(lock_descriptor)
            named = lock.lstat()
            if (
                _node(held) != lock_node
                or _node(named) != lock_node
                or not stat.S_ISREG(named.st_mode)
                or held.st_uid != os.geteuid()
                or stat.S_IMODE(held.st_mode) != 0o600
                or named.st_nlink != 1
                or named.st_size != held.st_size
            ):
                raise ValueError("html.tree.publication-lock-changed")
            if (
                _local_identity_wire(_node(_root(root).lstat()))
                != plan["output_plan"]["project_node"]
                or _local_identity_wire(_node(_root(source).lstat()))
                != plan["source_node"]
                or _local_identity_wire(_node(_root(output).lstat()))
                != plan["output_plan"]["output_node"]
            ):
                raise ValueError("html.tree.publication-target-changed")
            for path, expected in managed_nodes.items():
                if _private_directory(path) != expected:
                    raise ValueError("html.tree.state-custody-changed")
                require_cache_directory_current(path, kind="object", project_root=state)

        def require_current():
            require_paths()
            framework = _framework()
            if (
                framework.framework_distribution_identity.to_dict()
                != plan["framework_distribution_identity"]
                or framework.schema_catalog_release != plan["schema_catalog_release"]
                or _renderer_identity(framework) != renderer
            ):
                raise ValueError("html.tree.renderer-binding-changed")
            require_paths()

        require_current()
        if plan_html_tree_render(root, declaration) != plan:
            raise ValueError("html.tree.initial-plan-changed")
        require_current()
        for path in (managed, target):
            ensure_cache_directory(path, kind="object", project_root=state)
            managed_nodes[path] = _private_directory(path)
        if os.path.lexists(cas_root):
            raise ValueError("html.tree.initial-cas-already-exists")
        require_current()
        with initialize_html_tree_output(
            root,
            request,
            acknowledged_plan_identity=canonical_identity(plan["output_plan"]),
        ) as owner:
            # The identifiable output owner exists before any retained payload.
            require_current()
            os.mkdir(cas_root, 0o700)
            cas_node = _private_directory(cas_root)
            ensure_cache_directory(cas_root, kind="object", project_root=state)
            if _private_directory(cas_root) != cas_node:
                raise ValueError("html.tree.initial-cas-changed")
            managed_nodes[cas_root] = cas_node
            require_current()
            store = FileSystemCAS(cas_root)
            require_current()
            with observe_owned_html_tree_inputs(
                root, request, store, output_owner=owner
            ) as observed:
                require_current()
                staged = stage_html_tree_generation(
                    observed.tree,
                    request,
                    store,
                    reserved_memory_bytes=owner.retained_memory_bytes,
                )
                require_current()
                observed.require_current()
                require_current()
                owner.stage_generation(observed.tree, store, staged)
                require_current()
                owner.publish_generation(
                    source_observation=observed,
                    require_publication_current=require_current,
                )
                require_current()
                owner.require_current()
                snapshot = read_output(
                    root / request.index_path,
                    maximum_bytes=min(65536, request.limits.max_page_bytes),
                )
                if snapshot is None:
                    raise ValueError("html.tree.published-index-missing")
                owner.require_published_index(snapshot)
                observed.require_current(
                    reserved_memory_bytes=16 * len(snapshot.content)
                )
                require_current()
                result = {
                    "schema": "literate-ai/retained-tree-html-initial-result@1",
                    "plan_identity": acknowledged_plan_identity.uri,
                    "request_identity": request.identity.uri,
                    "source_identity": observed.tree.identity.uri,
                    "generation_manifest": staged.manifest.to_dict(),
                    "index_path": request.index_path,
                    "index_identity": "sha256:"
                    + hashlib.sha256(snapshot.content).hexdigest(),
                    "page_count": staged.page_count,
                    "page_bytes": staged.page_bytes,
                    "published": True,
                }
                del snapshot
            require_current()
        require_current()
    return result
