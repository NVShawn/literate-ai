"""Re-read bound refresh custody without rediscovering an unchanged Git graph.

Nothing is trusted on a stat fingerprint alone. Every retained file's complete
bytes and signature are rechecked. Includes, packed objects and shared worktree
registries use fresh Git observations; simple stores reuse no unchecked fact.
"""

from __future__ import annotations

import os

from literate_ai.project_authority_graph import project_authority_graph
from literate_ai.projects import ProjectError

from .project_validation import (
    ProjectValidationError,
    validated_project_authority_identity,
)
from .repository_locks import RepositoryLockStore
from .repository_orchestration import OrchestrationInventoryError, _git, _oid
from .repository_refresh import (
    _absent,
    _clean,
    _configuration,
    _configuration_file,
    _fail,
    _file,
    _node,
)
from .repository_refresh_directories import (
    observe_metadata_parents,
    optional_metadata_file,
    refresh_metadata_directory_targets,
)
from .repository_refresh_refs import _reference_file
from .repository_refresh_worktrees import observe_refresh_worktrees

_OPERATIONS = (
    "HEAD.lock",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "BISECT_LOG",
)


def _same(actual, expected):
    if actual != expected:
        _fail("inputs_changed", "refresh inputs changed after preparation")


def _retained_file(observed, *, maximum_bytes=16 * 1024 * 1024):
    _same(
        _file(
            observed.path,
            optional=observed.content is None,
            maximum_bytes=maximum_bytes,
        ),
        observed,
    )


def _git_custody(observed, *, clean, reservations):
    _same(_node(observed.root), observed.root_node)
    _same(_node(observed.git_directory), observed.git_node)
    _same(_node(observed.common_directory), observed.common_node)
    boundary = observed.root / ".git"
    if isinstance(observed.boundary, tuple):
        _same(_node(boundary), observed.boundary)
    else:
        _retained_file(observed.boundary)
    # A linked worktree can redirect its common store without changing .git.
    _retained_file(observed.common_locator, maximum_bytes=4096)
    for marker in _OPERATIONS:
        _absent(observed.git_directory / marker, reservations)
    _absent(
        observed.index.path.with_name(observed.index.path.name + ".lock"), reservations
    )
    _absent(observed.common_directory / "packed-refs.lock", reservations)
    # Compare complete index bytes before allowing any index-reading Git command.
    # The original index already passed split-index and canonical inventory checks.
    _retained_file(observed.index)
    _retained_file(observed.head, maximum_bytes=4096)
    _retained_file(observed.packed_references, maximum_bytes=1024 * 1024)
    for reference in observed.references:
        path = reference.file.path
        anchors = [
            anchor
            for anchor in (observed.git_directory, observed.common_directory)
            if path == anchor / reference.name
        ]
        if not anchors:
            _fail("refresh_refs_unsupported", "reference storage is redirected")
        _same(
            _reference_file(anchors[0], path.relative_to(anchors[0]), reservations),
            reference.file,
        )
    # Included configuration retains Git resolution; plain local/worktree settings
    # are completely bound by exact file signatures and fresh content digests.
    if observed.configuration_files is not None:
        # _git disables global/system configuration. Without includes, these
        # exact local/worktree files completely determine the admitted settings.
        for configuration in observed.configuration_files:
            _same(_configuration_file(configuration.path), configuration)
    else:
        _same(
            _configuration(observed.root, clean=clean), observed.configuration_identity
        )
    if observed.loose_commit is not None:
        _retained_file(observed.loose_commit)
    else:
        _same(
            _oid(_git(observed.root, "rev-parse", "--verify", "HEAD^{commit}").strip()),
            observed.commit,
        )
    if clean:
        _same(
            _clean(observed.root, reservations, index_flags_verified=True),
            observed.hydrated_lfs,
        )


def _worktrees(prepared, reservations):
    from .repository_refresh_worktrees import _OPERATIONS as markers

    observations = (prepared.root_git, *prepared.children)
    for registry in prepared.worktrees:
        group = tuple(
            item
            for item in observations
            if item.common_directory == registry.common_directory
        )
        rows = registry.worktrees
        if not (
            len(group) == len(rows) == len(registry.metadata_nodes) == 1
            and group[0].git_directory == registry.common_directory
            and not rows[0].bare
            and not rows[0].locked
            and not rows[0].prunable
            and rows[0].reference == group[0].symbolic_reference
            and rows[0].commit == group[0].commit
            and all(node is not None for _, node in registry.worktree_nodes)
        ):
            _same(observe_refresh_worktrees(group, reservations), (registry,))
            continue
        # A sole primary's commit/ref/locator were already rechecked above. Read
        # registration bytes, names and nodes; any peer forces full rediscovery.
        directory = registry.common_directory / "worktrees"
        if registry.directory_node is None:
            if os.path.lexists(directory):
                _fail("inputs_changed", "worktree registrations changed")
        else:
            _same(_node(directory), registry.directory_node)
            with os.scandir(directory) as entries:
                if next(entries, None) is not None:
                    _fail("inputs_changed", "worktree registrations changed")
        for path, node in (*registry.metadata_nodes, *registry.worktree_nodes):
            _same(_node(path), node)
        for observed in registry.files:
            _retained_file(observed, maximum_bytes=4096)
        busy = tuple(
            (path, marker)
            for path, _ in registry.metadata_nodes
            for marker in markers
            if not (reservations is not None and reservations.owns(path / marker))
            and os.path.lexists(path / marker)
        )
        _same(busy, registry.busy)


def revalidate_bound_refresh(prepared, reservations):
    root = prepared.repository.root
    store = RepositoryLockStore(root)
    _same(_node(store.directory), prepared.metadata_node)
    _absent(store.writer_path, reservations)
    _retained_file(prepared.manifest)
    _retained_file(prepared.repository_lock)
    _git_custody(prepared.root_git, clean=False, reservations=reservations)
    for child in prepared.children:
        _git_custody(child, clean=True, reservations=reservations)
    for modules in prepared.gitmodules:
        _retained_file(modules)
    # Full authority is recomputed: unchanged Git metadata does not imply unchanged
    # Component specifications, catalogs, lineage, locks or review authority.
    try:
        authority = validated_project_authority_identity(
            root, synchronize_source_intelligence=False
        )
        graph = project_authority_graph(root, include_component_locks=False)
    except (ProjectError, ProjectValidationError) as exc:
        raise OrchestrationInventoryError(
            "root_authority_invalid", f"root authority validation failed ({exc.code})"
        ) from exc
    _same(authority.uri, prepared.repository.lock.authority_identity)
    _same(graph.identity, prepared.repository.lock.authority_graph_identity)
    _worktrees(prepared, reservations)
    # The preceding fresh configuration digest fixes reflog policy and locators.
    # Re-read the original bounded files, including absent paths created by owners.
    for log in prepared.reflogs:
        _same(_node(log.anchor), log.anchor_node)
        _same(
            optional_metadata_file(
                log.before.path,
                log.anchor,
                log.anchor_node,
                maximum_bytes=16 * 1024 * 1024,
                reservations=reservations,
            ),
            log.before,
        )
        if log.before.path.parent.is_dir():
            _absent(
                log.before.path.with_name(log.before.path.name + ".lock"), reservations
            )
    directories = observe_metadata_parents(
        refresh_metadata_directory_targets(
            prepared.root_git,
            prepared.children,
            prepared.manifest,
            prepared.repository_lock,
            prepared.reflogs,
        ),
        reservations,
    )
    _same(directories, prepared.metadata_directories)
