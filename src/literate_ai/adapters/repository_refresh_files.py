"""Live-owner-bound physical custody; no replay or filesystem application API."""

from __future__ import annotations

from dataclasses import replace

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy

from .repository_file_custody import (
    PreparedWorktreeChanges,
    prepare_worktree_changes,
    require_worktree_changes_unchanged,
)
from .repository_local_tree import capture_local_repository_tree
from .repository_orchestration import OrchestrationInventoryError
from .repository_refresh_ownership import (
    OwnedRefreshPublication,
    refresh_publication_custody_identity,
)

_CREATION_KEY = object()


class PreparedRefreshFiles:
    """Immutable plan data bound to the still-live owner that captured it."""

    __slots__ = ("_owner", "_plans")

    def __init__(self, key, owner, plans):
        if key is not _CREATION_KEY:
            raise TypeError(
                "physical refresh custody must be prepared, not reconstructed"
            )
        self._owner = owner
        self._plans = plans

    @property
    def plans(self) -> tuple[tuple[str, PreparedWorktreeChanges], ...]:
        return self._plans

    @property
    def target_modes(self) -> tuple[tuple[str, str], ...]:
        return self._owner._prepared.target_modes

    @property
    def identity(self) -> str:
        plans = dict(self._plans)
        return canonical_identity(
            {
                "publication_custody": refresh_publication_custody_identity(
                    self._owner._prepared
                ),
                "targets": [
                    {
                        "path": path,
                        "mode": mode,
                        "physical_custody": (
                            plans[path].identity
                            if mode == "source-transition"
                            else None
                        ),
                    }
                    for path, mode in self.target_modes
                ],
            }
        ).uri

    def require_current(self) -> None:
        self._owner.require_inputs_unchanged()
        for _, plan in self._plans:
            require_worktree_changes_unchanged(plan)
        self._owner.require_inputs_unchanged()

    def stage(self, *, metadata=False):
        """Acquire temporary owned staging, without applying live changes."""
        from .repository_refresh_staging import stage_refresh_files

        return stage_refresh_files(self, metadata=metadata)

    def prepare_objects(self, *, pack_policy=None):
        """Export complete verified history under the still-live owner."""
        from .repository_refresh_objects import prepare_refresh_objects

        return prepare_refresh_objects(self, pack_policy=pack_policy)


def prepare_refresh_files(
    owner: OwnedRefreshPublication, *, policy: RepositoryTreeCapturePolicy | None = None
) -> PreparedRefreshFiles:
    if not isinstance(owner, OwnedRefreshPublication):
        raise TypeError("physical planning requires live refresh ownership")
    policy = RepositoryTreeCapturePolicy() if policy is None else policy
    if not isinstance(policy, RepositoryTreeCapturePolicy):
        raise TypeError("physical planning requires a typed capture policy")
    prospective = owner.capture_prospective_trees(tree_policy=policy)
    observations = {item.root: item for item in owner._prepared.refresh.children}
    root = owner._prepared.refresh.repository.root
    plans = []
    old_entries = old_metadata = old_bytes = physical_bytes = members = 0
    for path, captured in prospective:
        observed = observations[root / path]
        owner.require_inputs_unchanged()
        try:
            remaining = max(1, policy.maximum_total_blob_bytes - old_bytes)
            previous = capture_local_repository_tree(
                observed.root,
                observed.commit,
                policy=replace(
                    policy,
                    maximum_entries=max(1, policy.maximum_entries - old_entries),
                    maximum_metadata_bytes=max(
                        1, policy.maximum_metadata_bytes - old_metadata
                    ),
                    maximum_blob_bytes=min(policy.maximum_blob_bytes, remaining),
                    maximum_total_blob_bytes=remaining,
                ),
            )
            old_entries += len(previous.entries)
            old_metadata += previous.metadata_size_bound
            old_bytes += sum(len(entry.content or b"") for entry in previous.entries)
            if (
                old_entries > policy.maximum_entries
                or old_metadata > policy.maximum_metadata_bytes
                or old_bytes > policy.maximum_total_blob_bytes
            ):
                raise OrchestrationInventoryError(
                    "refresh_before_limit", "old-tree capture exceeds aggregate bounds"
                )
            physical_remaining = max(
                1, policy.maximum_total_blob_bytes - physical_bytes
            )
            plan = prepare_worktree_changes(
                observed.root,
                observed.root_node,
                previous,
                captured.tree,
                policy=replace(
                    policy,
                    maximum_entries=max(1, policy.maximum_entries - members),
                    maximum_blob_bytes=min(
                        policy.maximum_blob_bytes, physical_remaining
                    ),
                    maximum_total_blob_bytes=physical_remaining,
                ),
            )
            physical_bytes += plan.physical_bytes
            members += sum(len(directory.members) for directory in plan.directories)
            if (
                physical_bytes > policy.maximum_total_blob_bytes
                or members > policy.maximum_entries
            ):
                raise OrchestrationInventoryError(
                    "refresh_files_limit", "physical custody exceeds aggregate bounds"
                )
            plans.append((path, plan))
        finally:
            owner.require_inputs_unchanged()
    prepared = PreparedRefreshFiles(_CREATION_KEY, owner, tuple(plans))
    prepared.require_current()
    return prepared
