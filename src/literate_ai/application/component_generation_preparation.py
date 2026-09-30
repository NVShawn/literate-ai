"""Prepare independently generatable nodes from one exact Component execution plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from literate_ai.application.component_generation_context import (
    PreparedComponentGenerationRequest,
    PromptSegmentInput,
    prepare_component_generation_context,
)
from literate_ai.contracts.executable_components import (
    ComponentExecutionPlan,
    ComponentGenerationPlan,
    GenerationComplexityBudget,
    ReplacementWorkspace,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

AuthorityT = TypeVar("AuthorityT")
RecipeT = TypeVar("RecipeT")
DefinitionT = TypeVar("DefinitionT")


class ComponentGenerationPreparationError(ValueError):
    """A per-node projection did not match the exact execution-plan boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ComponentGenerationWorkspaceDescriptor:
    """One opaque, freshly allocated empty workspace for a single node attempt."""

    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    allocation_identity: ContentIdentity
    locator: str
    workspace: ReplacementWorkspace = ReplacementWorkspace.FRESH_EMPTY

    def __post_init__(self) -> None:
        identities = (
            self.component_revision,
            self.generation_plan_identity,
            self.generation_key_identity,
            self.allocation_identity,
        )
        if any(not isinstance(item, ContentIdentity) for item in identities):
            raise TypeError("workspace descriptor identities must be ContentIdentity")
        if not isinstance(self.locator, str) or not self.locator.strip():
            raise ComponentGenerationPreparationError(
                "component_preparation.workspace_locator_invalid",
                "workspace locator must be a non-empty opaque string",
            )
        if self.workspace is not ReplacementWorkspace.FRESH_EMPTY:
            raise ComponentGenerationPreparationError(
                "component_preparation.workspace_not_fresh",
                "Component generation requires a fresh empty workspace",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/component-generation-workspace@1",
                "component_revision": self.component_revision.uri,
                "generation_plan_identity": self.generation_plan_identity.uri,
                "generation_key_identity": self.generation_key_identity.uri,
                "allocation_identity": self.allocation_identity.uri,
                "locator": self.locator,
                "workspace": self.workspace.value,
            }
        )


@dataclass(frozen=True, slots=True)
class LockedNodeGenerationProjection(Generic[DefinitionT, RecipeT]):
    """Adapter projection containing only one node and direct public interfaces."""

    component_revision: ContentIdentity
    definition: DefinitionT
    recipe: RecipeT
    authority_segments: tuple[PromptSegmentInput, ...]


@dataclass(frozen=True, slots=True)
class PreparedComponentGenerationNode(Generic[DefinitionT, RecipeT]):
    """Complete independently runnable preparation for one planned Component."""

    plan: ComponentGenerationPlan
    definition: DefinitionT
    recipe: RecipeT
    request: PreparedComponentGenerationRequest
    workspace: ComponentGenerationWorkspaceDescriptor


def prepare_component_generation_nodes(
    execution_plan: ComponentExecutionPlan,
    *,
    authority: AuthorityT,
    authority_lock_identity: Callable[[AuthorityT], ContentIdentity],
    authority_guard: Callable[[AuthorityT], None],
    node_projector: Callable[
        [AuthorityT, ComponentGenerationPlan],
        LockedNodeGenerationProjection[DefinitionT, RecipeT],
    ],
    workspace_allocator: Callable[
        [ComponentGenerationPlan], ComponentGenerationWorkspaceDescriptor
    ],
    framework_envelope: bytes
    | Callable[[LockedNodeGenerationProjection[DefinitionT, RecipeT]], bytes],
    budget: GenerationComplexityBudget,
) -> tuple[PreparedComponentGenerationNode[DefinitionT, RecipeT], ...]:
    """Project, bound, and allocate every node without flattening graph authority."""

    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("execution_plan must be a ComponentExecutionPlan")
    if authority_lock_identity(authority) != execution_plan.component_lock_identity:
        raise ComponentGenerationPreparationError(
            "component_preparation.lock_mismatch",
            "locked authority and Component execution plan identify different locks",
        )
    authority_guard(authority)
    prepared: list[PreparedComponentGenerationNode[DefinitionT, RecipeT]] = []
    workspace_allocations: set[str] = set()
    workspace_locators: set[str] = set()
    for plan in execution_plan.generation_plans:
        authority_guard(authority)
        projection = node_projector(authority, plan)
        if projection.component_revision != plan.component_revision:
            raise ComponentGenerationPreparationError(
                "component_preparation.projection_component_mismatch",
                "node projection identifies a different Component revision",
            )
        node_envelope = (
            framework_envelope(projection)
            if callable(framework_envelope)
            else framework_envelope
        )
        request = prepare_component_generation_context(
            plan,
            framework_envelope=node_envelope,
            authority_segments=projection.authority_segments,
            budget=budget,
        )
        workspace = workspace_allocator(plan)
        if (
            workspace.component_revision != plan.component_revision
            or workspace.generation_plan_identity != plan.identity
            or workspace.generation_key_identity != plan.generation_key.identity
        ):
            raise ComponentGenerationPreparationError(
                "component_preparation.workspace_plan_mismatch",
                "fresh workspace descriptor does not bind its exact node plan",
            )
        allocation = workspace.allocation_identity.uri
        if (
            allocation in workspace_allocations
            or workspace.locator in workspace_locators
        ):
            raise ComponentGenerationPreparationError(
                "component_preparation.workspace_reused",
                "every Component node requires a distinct fresh workspace",
            )
        workspace_allocations.add(allocation)
        workspace_locators.add(workspace.locator)
        prepared.append(
            PreparedComponentGenerationNode(
                plan,
                projection.definition,
                projection.recipe,
                request,
                workspace,
            )
        )
    authority_guard(authority)
    return tuple(prepared)


__all__ = [
    "ComponentGenerationPreparationError",
    "ComponentGenerationWorkspaceDescriptor",
    "LockedNodeGenerationProjection",
    "PreparedComponentGenerationNode",
    "prepare_component_generation_nodes",
]
