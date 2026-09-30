"""Resolve SDK recipe authority from locked Flavor builder contributions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.contracts import ContributionKind, FlavorAxis, MergeOperator
from literate_ai.contracts.identity import (
    ContentIdentity,
    ContentReference,
    canonical_identity,
)
from literate_ai.contracts.native_sdks import (
    NATIVE_SDK_BUILD_RECIPE_CONTENT_KIND,
    NativeSdkBuildRecipe,
)
from literate_ai.contracts.repositories import RepositoryBuildPlan, RepositorySourceLock
from literate_ai.storage import FileSystemCAS


@dataclass(frozen=True, slots=True)
class NativeSdkRecipeSelection:
    component_revision: ContentIdentity
    target_identity: ContentIdentity
    flavor_revision: ContentIdentity
    content: ContentReference
    source_lock: RepositorySourceLock
    recipe: NativeSdkBuildRecipe

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/native-sdk-recipe-selection@1",
            "component_revision": self.component_revision.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "flavor_revision": self.flavor_revision.to_dict(),
            "content": self.content.to_dict(),
            "source_lock": self.source_lock.identity.to_dict(),
            "recipe": self.recipe.identity.to_dict(),
        }


def select_native_sdk_recipes(
    snapshot: LockedGenerationAuthoritySnapshot, component_revision: ContentIdentity
) -> tuple[NativeSdkRecipeSelection, ...]:
    """Read current locked recipe bytes; this grants no build or import admission."""
    snapshot.require_unchanged()
    node = next(
        (
            node
            for node in snapshot.authority.lock.nodes
            if node.revision.identity == component_revision
        ),
        None,
    )
    if node is None:
        raise ValueError("SDK recipe consumer is absent from the Component lock")
    sources = {
        item.dependency.dependency_id: item for item in node.revision.repository_sources
    }
    selected = set(node.revision.selected_flavor_revisions)
    flavors = tuple(
        flavor
        for flavor in snapshot.authority.selected_flavors
        if flavor.identity in selected
    )
    if {item.identity for item in flavors} != selected:
        raise ValueError("SDK recipe selected Flavor authority is incomplete")
    targets: dict[FlavorAxis, set[str]] = {}
    for flavor in flavors:
        targets.setdefault(flavor.definition.primary_axis, set()).update(
            flavor.definition.supported_targets
        )
    recipes: dict[str, NativeSdkRecipeSelection] = {}
    for flavor in flavors:
        for contribution in flavor.definition.contributions:
            if contribution.content.kind != NATIVE_SDK_BUILD_RECIPE_CONTENT_KIND:
                continue
            if (
                contribution.kind is not ContributionKind.BUILDER
                or contribution.merge_operator is not MergeOperator.EXACT_SINGLETON
            ):
                raise ValueError(
                    "SDK recipes require exact-singleton builder contributions"
                )
            recipe = NativeSdkBuildRecipe.from_dict(
                json.loads(
                    snapshot.flavor_content(flavor.identity, contribution.content)
                )
            )
            if contribution.slot != f"native-sdk:{recipe.dependency_id}":
                raise ValueError(
                    "SDK recipe contribution slot differs from its dependency"
                )
            if recipe.dependency_id not in sources:
                raise ValueError("SDK recipe has no authored repository dependency")
            if recipe.dependency_id in recipes:
                raise ValueError("SDK dependency has ambiguous selected recipes")
            source = sources[recipe.dependency_id]
            interface = source.dependency.integration_contract
            if (
                interface is None
                or interface.kind != "integration-contract"
                or any(
                    item.interface_identity != interface.identity
                    for item in recipe.layout.import_surface.capabilities
                )
            ):
                raise ValueError(
                    "SDK recipe imports differ from the locked integration contract"
                )
            for axis, value in (
                (FlavorAxis.PLATFORM_OS, recipe.layout.operating_system),
                (FlavorAxis.PLATFORM_ARCHITECTURE, recipe.layout.architecture),
                (
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                    recipe.layout.import_surface.language,
                ),
            ):
                if axis in targets and targets[axis] != {value}:
                    raise ValueError("SDK recipe target differs from selected Flavors")
            recipes[recipe.dependency_id] = NativeSdkRecipeSelection(
                component_revision,
                node.target_flavor_selection.identity,
                flavor.identity,
                contribution.content,
                source,
                recipe,
            )
    if set(recipes) != set(sources):
        raise ValueError("SDK recipes must cover every locked repository dependency")
    snapshot.require_unchanged()
    return tuple(recipes[name] for name in sorted(recipes))


class NativeSdkRecipePlanner:
    """Derive the existing repository build plan from current authored authority."""

    def __init__(
        self,
        *,
        snapshot: LockedGenerationAuthoritySnapshot,
        selection: NativeSdkRecipeSelection,
        tools: Mapping[str, LocalComponentToolBinding],
        store: FileSystemCAS,
    ) -> None:
        self.snapshot = snapshot
        self.selection = selection
        self.tools = dict(sorted(tools.items()))
        self.store = store

    def plan(
        self,
        lock: RepositorySourceLock,
        index_binding: ContentIdentity,
        effective_revision: ContentIdentity,
        flavor_set: ContentIdentity,
        toolchains: tuple[ContentIdentity, ...],
    ) -> RepositoryBuildPlan:
        selected = self.selection
        current = select_native_sdk_recipes(self.snapshot, selected.component_revision)
        if selected not in current or lock != selected.source_lock:
            raise ValueError(
                "SDK recipe plan differs from current source or recipe authority"
            )
        if (
            effective_revision != selected.component_revision
            or flavor_set != selected.target_identity
        ):
            raise ValueError("SDK recipe plan differs from its consumer target")
        if tuple(self.tools) != selected.recipe.tools:
            raise ValueError(
                "SDK recipe tools differ from resolved named tool bindings"
            )
        for tool in self.tools.values():
            tool.require_unchanged()
        if toolchains != tuple(tool.toolchain_identity for tool in self.tools.values()):
            raise ValueError("SDK recipe toolchain identities changed")
        decision = self.store.put_manifest(
            {
                "schema": "literate-ai/authored-native-sdk-build-decision@1",
                "selection": selected.identity.to_dict(),
                "recipe": selected.recipe.identity.to_dict(),
                "coding_provider_invoked": False,
            }
        )
        self.store.put_manifest(selected.to_dict())
        self.store.put_manifest(selected.recipe.to_dict())
        self.store.put_manifest(selected.recipe.layout.to_dict())
        return RepositoryBuildPlan(
            source_lock=lock.identity,
            effective_revision=effective_revision,
            flavor_set=flavor_set,
            evidence=(
                selected.identity,
                selected.recipe.identity,
                selected.recipe.layout.identity,
                index_binding,
            ),
            model_decision=ContentIdentity.parse_uri(decision.identity),
            toolchains=toolchains,
            commands=selected.recipe.commands,
            expected_outputs=(selected.recipe.layout.sdk_root,),
        )
