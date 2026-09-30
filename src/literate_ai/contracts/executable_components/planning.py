"""Deterministic per-Component execution and generation plan contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from .._validation import contract_fields, enum_value, fail, int_value, parse_tuple
from ..identity import ContentIdentity, contract_identity
from ._common import canonical_identities, identity, tuple_value
from .edges import ComponentActionPhase, ExecutableComponentEdge

COMPONENT_GENERATION_KEY_SCHEMA = "urn:literate-ai:schema:v2:component-generation-key"
COMPONENT_GENERATION_PLAN_SCHEMA = "urn:literate-ai:schema:v2:component-generation-plan"
COMPONENT_TOPOLOGICAL_LAYER_SCHEMA = (
    "urn:literate-ai:schema:v2:component-topological-layer"
)
COMPONENT_ACTION_PLAN_SCHEMA = "urn:literate-ai:schema:v2:component-action-plan"
COMPONENT_EXECUTION_PLAN_SCHEMA = "urn:literate-ai:schema:v2:component-execution-plan"


def _ordered_identities(
    value: object, path: str, *, required: bool = False
) -> tuple[ContentIdentity, ...]:
    values = tuple_value(value, path)
    if required and not values:
        fail(path, "must not be empty")
    if len(values) > 4096:
        fail(path, "must contain at most 4096 identities")
    parsed = tuple(
        identity(item, f"{path}[{index}]") for index, item in enumerate(values)
    )
    uris = tuple(item.uri for item in parsed)
    if len(set(uris)) != len(uris):
        fail(path, "must not repeat identities")
    return parsed


def _edge_key(edge: ExecutableComponentEdge) -> tuple[str, str, str, str]:
    return (
        edge.consumer_revision.uri,
        edge.provider_revision.uri,
        edge.requirement_id,
        edge.kind.value,
    )


def _canonical_edges(value: object, path: str) -> tuple[ExecutableComponentEdge, ...]:
    edges = tuple_value(value, path)
    if len(edges) > 16384 or any(
        not isinstance(item, ExecutableComponentEdge) for item in edges
    ):
        fail(path, "must contain at most 16384 executable Component edges")
    keys = tuple(_edge_key(item) for item in edges)
    if len(set(keys)) != len(keys):
        fail(path, "must not repeat executable Component edges")
    if keys != tuple(sorted(keys)):
        fail(path, "must use canonical executable Component edge order")
    return edges


@dataclass(frozen=True, slots=True)
class ComponentGenerationKey:
    """All and only identity-bearing inputs allowed to invalidate one node source."""

    specification_set_identity: ContentIdentity
    specification_identities: tuple[ContentIdentity, ...]
    flavor_selection_identity: ContentIdentity
    flavor_identities: tuple[ContentIdentity, ...]
    skill_identities: tuple[ContentIdentity, ...]
    workflow_identity: ContentIdentity
    routing_identity: ContentIdentity
    model_identity: ContentIdentity
    asset_identities: tuple[ContentIdentity, ...]
    exported_public_interface_identities: tuple[ContentIdentity, ...]
    direct_public_interface_identities: tuple[ContentIdentity, ...]
    provider_resolution_identities: tuple[ContentIdentity, ...] = ()
    native_sdk_input_identities: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = COMPONENT_GENERATION_KEY_SCHEMA

    def __post_init__(self) -> None:
        for field_name in (
            "specification_set_identity",
            "flavor_selection_identity",
            "workflow_identity",
            "routing_identity",
            "model_identity",
        ):
            identity(getattr(self, field_name), f"ComponentGenerationKey.{field_name}")
        _ordered_identities(
            self.specification_identities,
            "ComponentGenerationKey.specification_identities",
            required=True,
        )
        for field_name in (
            "flavor_identities",
            "skill_identities",
            "asset_identities",
            "exported_public_interface_identities",
            "direct_public_interface_identities",
            "provider_resolution_identities",
            "native_sdk_input_identities",
        ):
            canonical_identities(
                getattr(self, field_name), f"ComponentGenerationKey.{field_name}"
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "specification_set_identity": self.specification_set_identity.to_dict(),
            "specification_identities": [
                item.to_dict() for item in self.specification_identities
            ],
            "flavor_selection_identity": self.flavor_selection_identity.to_dict(),
            "flavor_identities": [item.to_dict() for item in self.flavor_identities],
            "skill_identities": [item.to_dict() for item in self.skill_identities],
            "workflow_identity": self.workflow_identity.to_dict(),
            "routing_identity": self.routing_identity.to_dict(),
            "model_identity": self.model_identity.to_dict(),
            "asset_identities": [item.to_dict() for item in self.asset_identities],
            "exported_public_interface_identities": [
                item.to_dict() for item in self.exported_public_interface_identities
            ],
            "direct_public_interface_identities": [
                item.to_dict() for item in self.direct_public_interface_identities
            ],
            "provider_resolution_identities": [
                item.to_dict() for item in self.provider_resolution_identities
            ],
            **(
                {
                    "native_sdk_input_identities": [
                        item.to_dict() for item in self.native_sdk_input_identities
                    ]
                }
                if self.native_sdk_input_identities
                else {}
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationKey"
    ) -> ComponentGenerationKey:
        names = frozenset(
            {
                "specification_set_identity",
                "specification_identities",
                "flavor_selection_identity",
                "flavor_identities",
                "skill_identities",
                "workflow_identity",
                "routing_identity",
                "model_identity",
                "asset_identities",
                "exported_public_interface_identities",
                "direct_public_interface_identities",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=names,
            optional=frozenset(
                {"provider_resolution_identities", "native_sdk_input_identities"}
            ),
        )
        singular = {
            name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
            for name in (
                "specification_set_identity",
                "flavor_selection_identity",
                "workflow_identity",
                "routing_identity",
                "model_identity",
            )
        }
        repeated = {
            name: parse_tuple(data[name], f"{path}.{name}", ContentIdentity.from_dict)
            for name in (
                "specification_identities",
                "flavor_identities",
                "skill_identities",
                "asset_identities",
                "exported_public_interface_identities",
                "direct_public_interface_identities",
            )
        }
        repeated["provider_resolution_identities"] = parse_tuple(
            data.get("provider_resolution_identities", ()),
            f"{path}.provider_resolution_identities",
            ContentIdentity.from_dict,
        )
        repeated["native_sdk_input_identities"] = parse_tuple(
            data.get("native_sdk_input_identities", ()),
            f"{path}.native_sdk_input_identities",
            ContentIdentity.from_dict,
        )
        return cls(**singular, **repeated)


@dataclass(frozen=True, slots=True)
class ComponentGenerationPlan:
    """One independently cacheable Component generation action."""

    component_graph_identity: ContentIdentity
    component_revision: ContentIdentity
    generation_key: ComponentGenerationKey
    direct_generation_edges: tuple[ExecutableComponentEdge, ...]

    SCHEMA: ClassVar[str] = COMPONENT_GENERATION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.component_graph_identity,
            "ComponentGenerationPlan.component_graph_identity",
        )
        identity(self.component_revision, "ComponentGenerationPlan.component_revision")
        if not isinstance(self.generation_key, ComponentGenerationKey):
            fail(
                "ComponentGenerationPlan.generation_key",
                "must be a ComponentGenerationKey",
            )
        edges = _canonical_edges(
            self.direct_generation_edges,
            "ComponentGenerationPlan.direct_generation_edges",
        )
        if any(
            edge.consumer_revision != self.component_revision
            or edge.semantics.consumer_phase is not ComponentActionPhase.GENERATE
            or not edge.semantics.visible_to_generation
            or edge.public_interface_identity is None
            for edge in edges
        ):
            fail(
                "ComponentGenerationPlan.direct_generation_edges",
                "must contain only complete direct generation-interface edges",
            )
        expected = tuple(
            sorted(
                {edge.public_interface_identity.uri for edge in edges},
            )
        )
        actual = tuple(
            item.uri for item in self.generation_key.direct_public_interface_identities
        )
        if actual != expected:
            fail(
                "ComponentGenerationPlan.generation_key",
                "must bind every and only direct public-interface identity",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_graph_identity": self.component_graph_identity.to_dict(),
            "component_revision": self.component_revision.to_dict(),
            "generation_key": self.generation_key.to_dict(),
            "direct_generation_edges": [
                item.to_dict() for item in self.direct_generation_edges
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationPlan"
    ) -> ComponentGenerationPlan:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_graph_identity",
                    "component_revision",
                    "generation_key",
                    "direct_generation_edges",
                }
            ),
        )
        return cls(
            component_graph_identity=ContentIdentity.from_dict(
                data["component_graph_identity"],
                path=f"{path}.component_graph_identity",
            ),
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            generation_key=ComponentGenerationKey.from_dict(
                data["generation_key"], path=f"{path}.generation_key"
            ),
            direct_generation_edges=parse_tuple(
                data["direct_generation_edges"],
                f"{path}.direct_generation_edges",
                ExecutableComponentEdge.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentTopologicalLayer:
    index: int
    component_revisions: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = COMPONENT_TOPOLOGICAL_LAYER_SCHEMA

    def __post_init__(self) -> None:
        int_value(
            self.index, "ComponentTopologicalLayer.index", minimum=0, maximum=4095
        )
        canonical_identities(
            self.component_revisions,
            "ComponentTopologicalLayer.component_revisions",
            required=True,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "index": self.index,
            "component_revisions": [
                item.to_dict() for item in self.component_revisions
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentTopologicalLayer"
    ) -> ComponentTopologicalLayer:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"index", "component_revisions"}),
        )
        return cls(
            int_value(data["index"], f"{path}.index", minimum=0, maximum=4095),
            parse_tuple(
                data["component_revisions"],
                f"{path}.component_revisions",
                ContentIdentity.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentActionPlan:
    """Stable provider-before-consumer layers for one lifecycle phase."""

    component_graph_identity: ContentIdentity
    phase: ComponentActionPhase
    component_revisions: tuple[ContentIdentity, ...]
    dependency_edges: tuple[ExecutableComponentEdge, ...]
    layers: tuple[ComponentTopologicalLayer, ...]

    SCHEMA: ClassVar[str] = COMPONENT_ACTION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.component_graph_identity,
            "ComponentActionPlan.component_graph_identity",
        )
        if not isinstance(self.phase, ComponentActionPhase):
            fail("ComponentActionPlan.phase", "must be a ComponentActionPhase")
        revisions = canonical_identities(
            self.component_revisions,
            "ComponentActionPlan.component_revisions",
            required=True,
        )
        edges = _canonical_edges(
            self.dependency_edges, "ComponentActionPlan.dependency_edges"
        )
        admitted = {item.uri for item in revisions}
        if any(
            edge.consumer_revision.uri not in admitted
            or edge.provider_revision.uri not in admitted
            or edge.semantics.consumer_phase is not self.phase
            for edge in edges
        ):
            fail(
                "ComponentActionPlan.dependency_edges",
                "must be closed and apply to the planned action phase",
            )
        layers = tuple_value(self.layers, "ComponentActionPlan.layers")
        if not layers or any(
            not isinstance(item, ComponentTopologicalLayer) for item in layers
        ):
            fail("ComponentActionPlan.layers", "must contain topological layers")
        if tuple(item.index for item in layers) != tuple(range(len(layers))):
            fail("ComponentActionPlan.layers", "must have contiguous indices from zero")
        positions: dict[str, int] = {}
        for layer in layers:
            for revision in layer.component_revisions:
                if revision.uri in positions:
                    fail(
                        "ComponentActionPlan.layers",
                        "must schedule each Component once",
                    )
                positions[revision.uri] = layer.index
        if set(positions) != admitted:
            fail("ComponentActionPlan.layers", "must schedule every and only Component")
        if any(
            positions[edge.provider_revision.uri]
            >= positions[edge.consumer_revision.uri]
            for edge in edges
        ):
            fail(
                "ComponentActionPlan.layers",
                "must schedule every dependency provider before its consumer",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_graph_identity": self.component_graph_identity.to_dict(),
            "phase": self.phase.value,
            "component_revisions": [
                item.to_dict() for item in self.component_revisions
            ],
            "dependency_edges": [item.to_dict() for item in self.dependency_edges],
            "layers": [item.to_dict() for item in self.layers],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentActionPlan"
    ) -> ComponentActionPlan:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_graph_identity",
                    "phase",
                    "component_revisions",
                    "dependency_edges",
                    "layers",
                }
            ),
        )
        return cls(
            component_graph_identity=ContentIdentity.from_dict(
                data["component_graph_identity"],
                path=f"{path}.component_graph_identity",
            ),
            phase=enum_value(ComponentActionPhase, data["phase"], f"{path}.phase"),
            component_revisions=parse_tuple(
                data["component_revisions"],
                f"{path}.component_revisions",
                ContentIdentity.from_dict,
            ),
            dependency_edges=parse_tuple(
                data["dependency_edges"],
                f"{path}.dependency_edges",
                ExecutableComponentEdge.from_dict,
            ),
            layers=parse_tuple(
                data["layers"], f"{path}.layers", ComponentTopologicalLayer.from_dict
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentExecutionPlan:
    """Complete deterministic Component DAG plan for one exact lock."""

    component_lock_identity: ContentIdentity
    root_revision: ContentIdentity
    planner_identity: ContentIdentity
    generation_plans: tuple[ComponentGenerationPlan, ...]
    action_plans: tuple[ComponentActionPlan, ...]

    SCHEMA: ClassVar[str] = COMPONENT_EXECUTION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        for field_name in (
            "component_lock_identity",
            "root_revision",
            "planner_identity",
        ):
            identity(getattr(self, field_name), f"ComponentExecutionPlan.{field_name}")
        generations = tuple_value(
            self.generation_plans, "ComponentExecutionPlan.generation_plans"
        )
        if not generations or any(
            not isinstance(item, ComponentGenerationPlan) for item in generations
        ):
            fail(
                "ComponentExecutionPlan.generation_plans",
                "must contain ComponentGenerationPlan values",
            )
        generation_uris = tuple(item.component_revision.uri for item in generations)
        if generation_uris != tuple(sorted(generation_uris)) or len(
            set(generation_uris)
        ) != len(generation_uris):
            fail(
                "ComponentExecutionPlan.generation_plans",
                "must use unique canonical Component revision order",
            )
        if any(
            item.component_graph_identity != self.component_lock_identity
            for item in generations
        ):
            fail(
                "ComponentExecutionPlan.generation_plans",
                "must bind the exact Component lock",
            )
        actions = tuple_value(self.action_plans, "ComponentExecutionPlan.action_plans")
        if any(not isinstance(item, ComponentActionPlan) for item in actions):
            fail(
                "ComponentExecutionPlan.action_plans",
                "must contain ComponentActionPlan values",
            )
        expected_phases = tuple(ComponentActionPhase)
        if tuple(item.phase for item in actions) != expected_phases:
            fail(
                "ComponentExecutionPlan.action_plans",
                "must contain every action phase in canonical order",
            )
        if any(
            item.component_graph_identity != self.component_lock_identity
            or tuple(value.uri for value in item.component_revisions) != generation_uris
            for item in actions
        ):
            fail(
                "ComponentExecutionPlan.action_plans",
                "must cover the exact generation-plan Component set and lock",
            )
        if self.root_revision.uri not in set(generation_uris):
            fail(
                "ComponentExecutionPlan.root_revision",
                "must identify a planned Component",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "root_revision": self.root_revision.to_dict(),
            "planner_identity": self.planner_identity.to_dict(),
            "generation_plans": [item.to_dict() for item in self.generation_plans],
            "action_plans": [item.to_dict() for item in self.action_plans],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentExecutionPlan"
    ) -> ComponentExecutionPlan:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_lock_identity",
                    "root_revision",
                    "planner_identity",
                    "generation_plans",
                    "action_plans",
                }
            ),
        )
        return cls(
            component_lock_identity=ContentIdentity.from_dict(
                data["component_lock_identity"], path=f"{path}.component_lock_identity"
            ),
            root_revision=ContentIdentity.from_dict(
                data["root_revision"], path=f"{path}.root_revision"
            ),
            planner_identity=ContentIdentity.from_dict(
                data["planner_identity"], path=f"{path}.planner_identity"
            ),
            generation_plans=parse_tuple(
                data["generation_plans"],
                f"{path}.generation_plans",
                ComponentGenerationPlan.from_dict,
            ),
            action_plans=parse_tuple(
                data["action_plans"],
                f"{path}.action_plans",
                ComponentActionPlan.from_dict,
            ),
        )


__all__ = [
    "COMPONENT_ACTION_PLAN_SCHEMA",
    "COMPONENT_EXECUTION_PLAN_SCHEMA",
    "COMPONENT_GENERATION_KEY_SCHEMA",
    "COMPONENT_GENERATION_PLAN_SCHEMA",
    "COMPONENT_TOPOLOGICAL_LAYER_SCHEMA",
    "ComponentActionPlan",
    "ComponentExecutionPlan",
    "ComponentGenerationKey",
    "ComponentGenerationPlan",
    "ComponentTopologicalLayer",
]
