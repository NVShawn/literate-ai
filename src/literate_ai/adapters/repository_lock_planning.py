"""Read-only preparation of root repository locks, with separate Git observations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_lock import RepositoryLock
from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
    RepositoryPin,
)
from literate_ai.project_authority_graph import project_authority_graph
from literate_ai.projects import ProjectConfigurationStore, ProjectError

from .project_validation import (
    ProjectValidationError,
    validated_project_authority_identity,
)
from .repository_orchestration import (
    GitlinkInventory,
    OrchestrationInventoryError,
    inspect_gitlink_inventory,
)


@dataclass(frozen=True, slots=True)
class PreparedRepositoryLock:
    """Local custody accompanies, but is excluded from, the portable lock."""

    root: Path
    root_node: tuple[int, int]
    manifest_content_identity: str
    lock: RepositoryLock
    inventory: GitlinkInventory

    def to_plan(self) -> dict[str, Any]:
        plan = {
            "schema": "literate-ai/repository-lock-plan@1",
            "repository_lock": self.lock.to_dict(),
            "repository_lock_identity": self.lock.identity,
            "inventory": self.inventory.to_dict(),
            "inventory_identity": self.inventory.identity,
            "writes": False,
            "execution": False,
            "execution_order": "not-inferred",
            "child_authority": "independent",
            "publication": "not-checked",
        }
        return {**plan, "plan_identity": canonical_identity(plan).uri}


def _root_node(root: Path) -> tuple[int, int]:
    require_safe_directory(root)
    metadata = root.lstat()
    return metadata.st_dev, metadata.st_ino


def _observe(root: Path) -> PreparedRepositoryLock:
    node = _root_node(root)
    snapshot = ProjectConfigurationStore(root).read()
    definition = snapshot.definition
    binding = definition.repository_orchestration
    if binding is None:
        raise OrchestrationInventoryError(
            "binding_required", "repository lock requires explicit root orchestration"
        )
    # Validate the root's own declared catalogs and review, never a child's catalogs.
    # No synchronization, generated code, model selection or publication probe occurs.
    authority = validated_project_authority_identity(
        root, synchronize_source_intelligence=False
    )
    graph = project_authority_graph(root, include_component_locks=False)
    inventory = inspect_gitlink_inventory(root)
    observed = RepositoryOrchestration(
        inventory.gitmodules_identity,
        tuple(
            RepositoryPin(child.name, child.path, child.url, child.commit, child.branch)
            for child in inventory.children
        ),
        binding.relationships,
    )
    if observed != binding:
        raise OrchestrationInventoryError(
            "binding_stale",
            "declared repository pins or configuration differ from the Git inventory; "
            "locking does not refresh authority",
        )
    if (
        ProjectConfigurationStore(root).read().content != snapshot.content
        or _root_node(root) != node
    ):
        raise OrchestrationInventoryError(
            "inputs_changed",
            "root authority changed during repository lock preparation",
        )
    return PreparedRepositoryLock(
        root,
        node,
        snapshot.content_identity.uri,
        RepositoryLock(
            definition.project_id,
            definition.identity.uri,
            authority.uri,
            graph.identity,
            binding,
        ),
        inventory,
    )


def _observe_checked(root: Path) -> PreparedRepositoryLock:
    try:
        return _observe(root)
    except OrchestrationInventoryError:
        raise
    except (ProjectError, ProjectValidationError) as exc:
        raise OrchestrationInventoryError(
            "root_authority_invalid", f"root authority validation failed ({exc.code})"
        ) from exc
    except (OSError, ValueError, TypeError, UnsafeFilesystemPathError) as exc:
        raise OrchestrationInventoryError(
            "lock_inputs_invalid", "repository lock inputs are unavailable or invalid"
        ) from exc


def prepare_repository_lock(root: Path) -> PreparedRepositoryLock:
    """Prepare one coherent root candidate without writing a lock or changing pins."""
    root = Path(root).absolute()
    prepared = _observe_checked(root)
    require_repository_lock_inputs_unchanged(prepared)
    return prepared


def require_repository_lock_inputs_unchanged(prepared: PreparedRepositoryLock) -> None:
    """Reobserve all authority and local inventory before a later custody action."""
    if not isinstance(prepared, PreparedRepositoryLock):
        raise TypeError("repository lock revalidation requires a prepared observation")
    if _observe_checked(prepared.root) != prepared:
        raise OrchestrationInventoryError(
            "inputs_changed", "repository lock inputs changed after preparation"
        )
