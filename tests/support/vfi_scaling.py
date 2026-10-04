"""VFI-shaped Component graph fixture for bounded-context scaling tests."""

from __future__ import annotations

from dataclasses import replace

from literate_ai.contracts.capabilities import CapabilityRequirement, DependencyKind
from literate_ai.contracts.component_locking import (
    ComponentLock,
    RequirementConstraintSatisfaction,
    ordered_specification_set_identity,
)
from literate_ai.contracts.executable_components import ExecutableComponentEdge
from tests.support.fixtures_test_component_lock_contracts import (
    component_authoring,
    identity,
    locked_revision,
    node,
)

FIXED_FEATURE = "feature-000"
DIRECT_PROVIDER = "feature-001"
PRIVATE_DESCENDANT = "feature-000-private"


def _capability(name: str) -> str:
    return f"{name}-api"


def _requirement(
    name: str, *, kind: DependencyKind = DependencyKind.GENERATION
) -> CapabilityRequirement:
    return CapabilityRequirement(name, _capability(name), ">=1,<2", kind)


def _revision(
    name: str,
    *,
    authoring: object,
    specification_label: str,
    interface_label: str,
):
    revision = locked_revision(
        name,
        selected_flavor="python",
        interface=(_capability(name), interface_label),
        authoring=authoring,
    )
    specifications = (
        replace(revision.specifications[0], identity=identity(specification_label)),
    )
    return replace(
        revision,
        specifications=specifications,
        specification_set_identity=ordered_specification_set_identity(
            authoring.specification_provider, specifications
        ),
    )


def vfi_component_lock(
    *,
    sibling_count: int = 50,
    sibling_order: tuple[str, ...] | None = None,
    specification_overrides: dict[str, str] | None = None,
    interface_overrides: dict[str, str] | None = None,
) -> ComponentLock:
    """Build one app with many sibling features and one private descendant.

    ``feature-000`` consumes ``feature-001`` during generation and consumes its
    private descendant only during build.  The root composes every sibling, making
    additions and edits realistic application-graph changes while leaving the fixed
    feature's generation boundary independently observable.
    """

    if sibling_count < 2:
        raise ValueError("the VFI fixture requires at least two sibling features")
    names = tuple(f"feature-{index:03d}" for index in range(sibling_count))
    ordered_names = names if sibling_order is None else sibling_order
    if len(ordered_names) != len(names) or set(ordered_names) != set(names):
        raise ValueError("sibling_order must contain every sibling exactly once")
    specification_overrides = specification_overrides or {}
    interface_overrides = interface_overrides or {}

    root_requirements = tuple(_requirement(name) for name in sorted(ordered_names))
    root_authoring = component_authoring("vfi-app", requirements=root_requirements)
    authorings = {"vfi-app": root_authoring}
    for name in ordered_names:
        requirements = ()
        if name == FIXED_FEATURE:
            requirements = tuple(
                sorted(
                    (
                        _requirement(DIRECT_PROVIDER),
                        _requirement(PRIVATE_DESCENDANT, kind=DependencyKind.BUILD),
                    ),
                    key=lambda item: item.requirement_id,
                )
            )
        authorings[name] = component_authoring(
            name,
            interface=(_capability(name), "selector-label"),
            requirements=requirements,
        )
    authorings[PRIVATE_DESCENDANT] = component_authoring(
        PRIVATE_DESCENDANT,
        interface=(_capability(PRIVATE_DESCENDANT), "selector-label"),
    )

    root_revision = locked_revision(
        "vfi-app", selected_flavor="python", authoring=root_authoring
    )
    nodes = {"vfi-app": node(root_revision, selected_flavor="python")}
    for name in (*ordered_names, PRIVATE_DESCENDANT):
        revision = _revision(
            name,
            authoring=authorings[name],
            specification_label=specification_overrides.get(
                name, f"{name}-specification"
            ),
            interface_label=interface_overrides.get(name, f"{name}-interface"),
        )
        nodes[name] = node(
            revision,
            selected_flavor="python",
            capability=_capability(name),
        )

    relationships = [
        ("vfi-app", name, name, DependencyKind.GENERATION) for name in ordered_names
    ]
    relationships.extend(
        (
            (
                FIXED_FEATURE,
                DIRECT_PROVIDER,
                DIRECT_PROVIDER,
                DependencyKind.GENERATION,
            ),
            (
                FIXED_FEATURE,
                PRIVATE_DESCENDANT,
                PRIVATE_DESCENDANT,
                DependencyKind.BUILD,
            ),
        )
    )
    satisfactions: dict[str, list[RequirementConstraintSatisfaction]] = {
        name: [] for name in nodes
    }
    edges: list[ExecutableComponentEdge] = []
    for consumer_name, provider_name, requirement_id, kind in relationships:
        consumer = nodes[consumer_name]
        provider = nodes[provider_name]
        requirement = next(
            item
            for item in authorings[consumer_name].requires
            if item.requirement_id == requirement_id
        )
        public_interface_identity = (
            provider.interface_bindings[0].interface_identity
            if kind is DependencyKind.GENERATION
            else None
        )
        edges.append(
            ExecutableComponentEdge(
                consumer.revision.identity,
                provider.revision.identity,
                requirement_id,
                requirement.capability,
                kind,
                public_interface_identity,
            )
        )
        selection = consumer.target_flavor_selection
        satisfactions[consumer_name].append(
            RequirementConstraintSatisfaction(
                consumer.revision.identity,
                provider.revision.identity,
                requirement_id,
                requirement.constraints,
                selection.target_name,
                selection.target_profile_identity,
                selection.selection_policy_identity,
                selection.identity,
                identity(f"{consumer_name}-{requirement_id}-satisfaction"),
            )
        )

    for name, values in satisfactions.items():
        nodes[name] = replace(
            nodes[name],
            requirement_constraint_satisfactions=tuple(
                sorted(values, key=lambda item: item.requirement_id)
            ),
        )
    return ComponentLock(
        target_name="host",
        target_profile_identity=identity("invoice-cli-target"),
        selection_policy_identity=identity("selection-policy"),
        resolver_identity=identity("component-lock-resolver"),
        root_revision=nodes["vfi-app"].revision.identity,
        nodes=tuple(
            sorted(nodes.values(), key=lambda item: item.revision.identity.uri)
        ),
        edges=tuple(
            sorted(
                edges,
                key=lambda item: (
                    item.consumer_revision.uri,
                    item.provider_revision.uri,
                    item.requirement_id,
                    item.kind.value,
                ),
            )
        ),
        _authorings=tuple(
            sorted(authorings.values(), key=lambda item: item.identity.uri)
        ),
    )
