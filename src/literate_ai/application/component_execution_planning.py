"""Pure planning service for independently generatable Component DAG nodes."""

from __future__ import annotations

from collections.abc import Mapping

from literate_ai.authority_graph import (
    AuthorityGraph,
    AuthorityGraphEdge,
    AuthorityGraphError,
    AuthorityGraphNode,
)
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.executable_components import (
    AuthoredBinaryAsset,
    ComponentActionPhase,
    ComponentActionPlan,
    ComponentExecutionPlan,
    ComponentGenerationKey,
    ComponentGenerationPlan,
    ComponentTopologicalLayer,
    ExecutableComponentEdge,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

COMPONENT_EXECUTION_PLANNER_IDENTITY = canonical_identity(
    {
        "schema": "literate-ai/component-execution-planner@1",
        "topology": "stable-provider-before-consumer-layers",
        "generation_visibility": "local-authority-plus-direct-public-interfaces",
    }
)


class ComponentExecutionPlanningError(ValueError):
    """A stable pre-generation failure in the locked Component DAG."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def authored_assets_from_lock(lock: ComponentLock) -> tuple[AuthoredBinaryAsset, ...]:
    """Project locked asset selectors into the explicit planning argument.

    Preparation reconstructs the same ``AuthoredBinaryAsset`` identities from the
    lock. ``ApplicationService.plan`` callers that omit ``assets`` must still bind
    those identities or fail closed as ``generation_key_mismatch`` on ``assets``.
    """

    if not isinstance(lock, ComponentLock):
        raise TypeError("authored assets require a ComponentLock")
    assets = [
        AuthoredBinaryAsset(
            component_revision=node.revision.identity,
            asset_id=resolved.selector.asset_id,
            path=resolved.selector.path,
            role=resolved.selector.role,
            target_identity=node.target_flavor_selection.identity,
            blob=resolved.blob,
            authorization_identity=lock.identity,
        )
        for node in lock.nodes
        for resolved in node.revision.assets
    ]
    return tuple(sorted(assets, key=lambda item: item.identity.uri))


def _edge_key(edge: ExecutableComponentEdge) -> tuple[str, str, str, str]:
    return (
        edge.consumer_revision.uri,
        edge.provider_revision.uri,
        edge.requirement_id,
        edge.kind.value,
    )


def _layers(
    revisions: tuple[ContentIdentity, ...],
    edges: tuple[ExecutableComponentEdge, ...],
) -> tuple[ComponentTopologicalLayer, ...]:
    """Return stable provider-before-consumer layers, rejecting a cycle."""

    identities = {item.uri: item for item in revisions}
    try:
        graph = AuthorityGraph.create(
            "component-execution",
            (
                AuthorityGraphNode(
                    item.uri,
                    "component-revision",
                    item.digest[:12],
                    "component-lock",
                    item.uri,
                    inheritable=False,
                    properties=(("identity", item.uri),),
                )
                for item in revisions
            ),
            (
                AuthorityGraphEdge(
                    edge.provider_revision.uri,
                    edge.consumer_revision.uri,
                    "component-dependency",
                    edge.requirement_id,
                )
                for edge in edges
            ),
        )
    except AuthorityGraphError as exc:
        if exc.code == "graph.edge_unresolved":
            raise ComponentExecutionPlanningError(
                "component_plan.graph_open",
                "an action edge references a Component outside the exact lock",
            ) from exc
        if exc.code == "graph.cycle":
            raise ComponentExecutionPlanningError(
                "component_plan.graph_cyclic",
                "Component action dependencies are cyclic: "
                + exc.message.removeprefix("directed cycle detected: "),
            ) from exc
        raise ComponentExecutionPlanningError(exc.code, exc.message) from exc
    return tuple(
        ComponentTopologicalLayer(
            index,
            tuple(identities[revision] for revision in layer),
        )
        for index, layer in enumerate(graph.topological_layers())
    )


def _require_generation_interfaces(
    lock: ComponentLock,
    edges: tuple[ExecutableComponentEdge, ...],
) -> None:
    nodes = {item.revision.identity.uri: item for item in lock.nodes}
    for edge in edges:
        interface = edge.public_interface_identity
        if interface is None:
            raise ComponentExecutionPlanningError(
                "component_plan.interface_missing",
                f"generation requirement {edge.requirement_id!r} has no public "
                "interface",
            )
        provider = nodes.get(edge.provider_revision.uri)
        if provider is None:
            raise ComponentExecutionPlanningError(
                "component_plan.interface_provider_missing",
                f"generation requirement {edge.requirement_id!r} has no locked "
                "provider",
            )
        matches = tuple(
            binding
            for binding in provider.interface_bindings
            if binding.capability == edge.capability
            and binding.interface_identity == interface
        )
        if len(matches) != 1:
            raise ComponentExecutionPlanningError(
                "component_plan.interface_incomplete",
                f"generation requirement {edge.requirement_id!r} does not select one "
                "exact provider public interface",
            )


def plan_component_execution(
    lock: ComponentLock,
    *,
    model_identities: Mapping[str, ContentIdentity],
    assets: tuple[AuthoredBinaryAsset, ...] = (),
    native_sdk_input_identities: Mapping[str, tuple[ContentIdentity, ...]]
    | None = None,
) -> ComponentExecutionPlan:
    """Project one exact lock into per-node keys and stable action layers.

    ``model_identities`` is keyed by locked Component revision URI. Asset and model
    selection are explicit arguments because they are selected after Component locking.
    Dependency source, build, test, and private specification identities are never
    projected into a consumer generation key. Direct SDK input IDs are explicit
    post-admission metadata; this pure plan does not grant SDK custody or execution.
    """

    if not isinstance(lock, ComponentLock):
        raise TypeError("Component execution planning requires a ComponentLock")
    sdk_owners = {
        node.revision.identity.uri: len(node.revision.repository_sources)
        for node in lock.nodes
        if node.revision.repository_sources
    }
    if sdk_owners and native_sdk_input_identities is None:
        raise ComponentExecutionPlanningError(
            "component_plan.repository_source_admission_required",
            "repository source locks require SDK build admission before generation",
        )
    sdk_inputs = (
        {} if native_sdk_input_identities is None else native_sdk_input_identities
    )
    if (
        not isinstance(sdk_inputs, Mapping)
        or set(sdk_inputs) != set(sdk_owners)
        or any(
            not isinstance(values, tuple)
            or len(values) != sdk_owners[owner]
            or any(not isinstance(value, ContentIdentity) for value in values)
            or values != tuple(sorted(set(values), key=lambda value: value.uri))
            for owner, values in sdk_inputs.items()
        )
    ):
        raise ComponentExecutionPlanningError(
            "component_plan.native_sdk_inputs_incomplete",
            "SDK input IDs must cover every direct repository dependency owner",
        )
    revisions = tuple(
        sorted(
            (item.revision.identity for item in lock.nodes), key=lambda item: item.uri
        )
    )
    admitted = {item.uri for item in revisions}
    if set(model_identities) != admitted or any(
        not isinstance(value, ContentIdentity) for value in model_identities.values()
    ):
        raise ComponentExecutionPlanningError(
            "component_plan.model_incomplete",
            "model identities must cover every and only locked Component revision",
        )
    asset_groups: dict[str, list[ContentIdentity]] = {uri: [] for uri in admitted}
    for asset in assets:
        if not isinstance(asset, AuthoredBinaryAsset):
            raise TypeError(
                "Component execution assets must be AuthoredBinaryAsset values"
            )
        uri = asset.component_revision.uri
        if uri not in admitted:
            raise ComponentExecutionPlanningError(
                "component_plan.asset_unknown_component",
                "an authored asset targets a Component outside the exact lock",
            )
        asset_groups[uri].append(asset.identity)

    generation_edges = tuple(
        sorted(
            (
                edge
                for edge in lock.edges
                if edge.semantics.consumer_phase is ComponentActionPhase.GENERATE
                and edge.semantics.visible_to_generation
            ),
            key=_edge_key,
        )
    )
    _require_generation_interfaces(lock, generation_edges)
    # Recompute topology even though ComponentLock validates acyclicity. The plan is a
    # separate trust boundary and must reject a corrupted/deserialized lock before
    # egress.
    _layers(revisions, tuple(sorted(lock.edges, key=_edge_key)))

    generation_plans: list[ComponentGenerationPlan] = []
    for node in sorted(lock.nodes, key=lambda item: item.revision.identity.uri):
        revision = node.revision
        revision_uri = revision.identity.uri
        direct_edges = tuple(
            edge
            for edge in generation_edges
            if edge.consumer_revision.uri == revision_uri
        )
        direct_interfaces = tuple(
            sorted(
                {
                    edge.public_interface_identity
                    for edge in direct_edges
                    if edge.public_interface_identity is not None
                },
                key=lambda item: item.uri,
            )
        )
        skills = tuple(
            sorted(
                (
                    item.identity
                    for item in revision.authoring_inputs
                    if item.kind == "specification-to-source-skill"
                ),
                key=lambda item: item.uri,
            )
        )
        key = ComponentGenerationKey(
            specification_set_identity=revision.specification_set_identity,
            specification_identities=tuple(
                item.identity for item in revision.specifications
            ),
            flavor_selection_identity=canonical_identity(
                {
                    "schema": "literate-ai/node-generation-flavor-selection@1",
                    "target_name": node.target_flavor_selection.target_name,
                    "slots": [
                        {
                            "slot_id": slot.slot.slot_id,
                            "flavor_revisions": [
                                selected.flavor_revision.uri
                                for selected in slot.selected
                            ],
                        }
                        for slot in node.target_flavor_selection.slots
                    ],
                }
            ),
            flavor_identities=tuple(
                sorted(revision.selected_flavor_revisions, key=lambda item: item.uri)
            ),
            skill_identities=skills,
            workflow_identity=revision.workflow_definition.identity,
            routing_identity=revision.routing_policy.identity,
            model_identity=model_identities[revision_uri],
            asset_identities=tuple(
                sorted(asset_groups[revision_uri], key=lambda item: item.uri)
            ),
            exported_public_interface_identities=tuple(
                sorted(
                    (item.identity for item in revision.public_interfaces),
                    key=lambda item: item.uri,
                )
            ),
            direct_public_interface_identities=direct_interfaces,
            provider_resolution_identities=tuple(
                item.identity for item in lock.provider_resolutions
            ),
            native_sdk_input_identities=sdk_inputs.get(revision_uri, ()),
        )
        generation_plans.append(
            ComponentGenerationPlan(
                component_graph_identity=lock.identity,
                component_revision=revision.identity,
                generation_key=key,
                direct_generation_edges=direct_edges,
            )
        )

    action_plans = tuple(
        ComponentActionPlan(
            component_graph_identity=lock.identity,
            phase=phase,
            component_revisions=revisions,
            dependency_edges=(
                phase_edges := tuple(
                    sorted(
                        (
                            edge
                            for edge in lock.edges
                            if edge.semantics.consumer_phase is phase
                        ),
                        key=_edge_key,
                    )
                )
            ),
            layers=_layers(revisions, phase_edges),
        )
        for phase in ComponentActionPhase
    )
    return ComponentExecutionPlan(
        component_lock_identity=lock.identity,
        root_revision=lock.root_revision,
        planner_identity=COMPONENT_EXECUTION_PLANNER_IDENTITY,
        generation_plans=tuple(generation_plans),
        action_plans=action_plans,
    )


__all__ = [
    "COMPONENT_EXECUTION_PLANNER_IDENTITY",
    "ComponentExecutionPlanningError",
    "authored_assets_from_lock",
    "plan_component_execution",
]
