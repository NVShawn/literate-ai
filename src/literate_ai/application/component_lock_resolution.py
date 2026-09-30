"""Pure deterministic application service for selected Component locks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from literate_ai.contracts.capabilities import (
    CapabilityConstraint,
    CapabilityRequirement,
    DependencyKind,
)
from literate_ai.contracts.component_locking.authoring import ComponentAuthoring
from literate_ai.contracts.component_locking.locks import (
    ComponentLock,
    ComponentLockNode,
    ComponentResolutionAudit,
    LockedComponentRevision,
    LockedFlavorRequirement,
    NodeFlavorCandidateAudit,
    RequirementConstraintSatisfaction,
    ResolvedComponentAsset,
    ordered_specification_set_identity,
)
from literate_ai.contracts.executable_components.edges import ExecutableComponentEdge
from literate_ai.contracts.executable_components.interfaces import (
    ComponentInterfaceBinding,
)
from literate_ai.contracts.executable_components.selection import (
    NodeFlavorSlotResolution,
    NodeTargetFlavorSelection,
    SelectedNodeFlavor,
)
from literate_ai.contracts.flavors import CandidateStatus
from literate_ai.contracts.identity import (
    ContentIdentity,
    ContentReference,
    canonical_identity,
)
from literate_ai.contracts.provider_resolution import ProviderResolution
from literate_ai.contracts.repositories import RepositorySourceLock

_PLAN_SCHEMA = "literate-ai/component-lock-resolution-plan@1"


class ComponentLockResolutionError(ValueError):
    """Exact resolver inputs are stale, incomplete, ambiguous, or contradictory."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _wire_fields(
    value: Any,
    *,
    path: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    schema: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ComponentLockResolutionError(
            "invalid-resolution-plan-wire", f"{path} must be an object"
        )
    expected = required | optional | ({"schema"} if schema is not None else set())
    unknown = set(value) - expected
    missing = required - value.keys()
    if schema is not None and "schema" not in value:
        missing.add("schema")
    if unknown:
        raise ComponentLockResolutionError(
            "invalid-resolution-plan-wire",
            f"{path} has unknown fields: {', '.join(sorted(unknown))}",
        )
    if missing:
        raise ComponentLockResolutionError(
            "invalid-resolution-plan-wire",
            f"{path} is missing required fields: {', '.join(sorted(missing))}",
        )
    if schema is not None and value["schema"] != schema:
        raise ComponentLockResolutionError(
            "unsupported-resolution-plan-schema",
            f"{path}.schema must be {schema!r}",
        )
    return value


def _wire_string(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComponentLockResolutionError(
            "invalid-resolution-plan-wire", f"{path} must be a non-empty string"
        )
    return value


def _wire_list(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ComponentLockResolutionError(
            "invalid-resolution-plan-wire", f"{path} must be an array"
        )
    return value


def _wire_strings(value: Any, *, path: str) -> tuple[str, ...]:
    return tuple(
        _wire_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(_wire_list(value, path=path))
    )


def _wire_identity(value: Any, *, path: str) -> ContentIdentity:
    try:
        return ContentIdentity.from_dict(value, path=path)
    except (TypeError, ValueError) as exc:
        raise ComponentLockResolutionError(
            "invalid-resolution-plan-wire", f"{path} is not a ContentIdentity"
        ) from exc


@dataclass(frozen=True, slots=True)
class CatalogAttribute:
    """One exact catalog attribute used by capability-constraint evaluation."""

    key: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.key or not self.values or len(set(self.values)) != len(self.values):
            raise ComponentLockResolutionError(
                "invalid-catalog-attribute",
                "catalog attributes require a key and unique non-empty values",
            )
        if any(not value for value in self.values):
            raise ComponentLockResolutionError(
                "invalid-catalog-attribute", "catalog attribute values cannot be empty"
            )

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key, "values": sorted(self.values)}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CatalogAttribute"
    ) -> CatalogAttribute:
        data = _wire_fields(
            value,
            path=path,
            required=frozenset({"key", "values"}),
        )
        return cls(
            key=_wire_string(data["key"], path=f"{path}.key"),
            values=_wire_strings(data["values"], path=f"{path}.values"),
        )


@dataclass(frozen=True, slots=True)
class FlavorCandidateInput:
    """One catalog candidate decision for one Component Flavor slot."""

    slot_id: str
    value: str
    flavor_revision: ContentIdentity
    status: CandidateStatus
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.slot_id or not self.value:
            raise ComponentLockResolutionError(
                "invalid-flavor-candidate", "Flavor candidate names cannot be empty"
            )
        if not isinstance(self.flavor_revision, ContentIdentity) or not isinstance(
            self.status, CandidateStatus
        ):
            raise TypeError(
                "Flavor candidates require typed identity and status values"
            )
        if self.status is CandidateStatus.SELECTED and self.reasons:
            raise ComponentLockResolutionError(
                "invalid-flavor-candidate",
                "selected Flavor candidates cannot have rejection reasons",
            )
        if self.status is not CandidateStatus.SELECTED and not self.reasons:
            raise ComponentLockResolutionError(
                "invalid-flavor-candidate",
                "unselected Flavor candidates require reasons",
            )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "slot_id": self.slot_id,
            "value": self.value,
            "flavor_revision": self.flavor_revision.to_dict(),
            "status": self.status.value,
            "reasons": sorted(self.reasons),
        }
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "FlavorCandidateInput"
    ) -> FlavorCandidateInput:
        data = _wire_fields(
            value,
            path=path,
            required=frozenset(
                {"slot_id", "value", "flavor_revision", "status", "reasons"}
            ),
        )
        raw_status = _wire_string(data["status"], path=f"{path}.status")
        try:
            status = CandidateStatus(raw_status)
        except ValueError as exc:
            raise ComponentLockResolutionError(
                "invalid-resolution-plan-wire",
                f"{path}.status is not a CandidateStatus",
            ) from exc
        reasons = _wire_strings(data["reasons"], path=f"{path}.reasons")
        if len(set(reasons)) != len(reasons):
            raise ComponentLockResolutionError(
                "invalid-resolution-plan-wire",
                f"{path}.reasons cannot contain duplicates",
            )
        return cls(
            slot_id=_wire_string(data["slot_id"], path=f"{path}.slot_id"),
            value=_wire_string(data["value"], path=f"{path}.value"),
            flavor_revision=_wire_identity(
                data["flavor_revision"], path=f"{path}.flavor_revision"
            ),
            status=status,
            reasons=reasons,
        )


@dataclass(frozen=True, slots=True)
class ResolvedComponentNodeInput:
    """Exact resolver-owned inputs for one human-authored Component revision."""

    authoring: ComponentAuthoring
    specifications: tuple[ContentReference, ...]
    authoring_inputs: tuple[ContentReference, ...]
    workflow_definition: ContentReference
    routing_policy: ContentReference
    acceptance_contracts: tuple[ContentReference, ...]
    repository_sources: tuple[RepositorySourceLock, ...]
    public_interfaces: tuple[ContentReference, ...]
    flavor_candidates: tuple[FlavorCandidateInput, ...]
    catalog_attributes: tuple[CatalogAttribute, ...] = ()
    assets: tuple[ResolvedComponentAsset, ...] = ()
    flavor_requirements: tuple[LockedFlavorRequirement, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.authoring, ComponentAuthoring):
            raise TypeError("resolved Component node requires ComponentAuthoring")
        authored_sources = {
            item.dependency_id: item for item in self.authoring.source_dependencies
        }
        locked_ids = tuple(
            item.dependency.dependency_id for item in self.repository_sources
        )
        if len(set(locked_ids)) != len(locked_ids) or set(locked_ids) != set(
            authored_sources
        ):
            raise ComponentLockResolutionError(
                "repository-source-selection-mismatch",
                "source locks must exactly cover authored repository dependencies",
            )
        for lock in self.repository_sources:
            selected = lock.dependency
            authored = authored_sources[selected.dependency_id]
            reference = selected.integration_contract
            selector = authored.integration_contract
            if (
                selected.repository_url != authored.repository_url
                or selected.revision_selector != authored.revision_selector
                or selected.dependency_kind != authored.dependency_kind
                or selected.optional != authored.optional
                or ((reference is None) != (selector is None))
                or (
                    reference is not None
                    and selector is not None
                    and (
                        reference.kind != selector.kind
                        or reference.uri != selector.uri
                        or (
                            selector.pin is not None
                            and reference.identity != selector.pin
                        )
                    )
                )
            ):
                raise ComponentLockResolutionError(
                    "repository-source-selection-mismatch",
                    "source lock differs from its authored dependency selector",
                )
        keys = tuple(
            (item.slot_id, item.flavor_revision.uri) for item in self.flavor_candidates
        )
        if len(set(keys)) != len(keys):
            raise ComponentLockResolutionError(
                "duplicate-flavor-candidate",
                "a node cannot repeat one Flavor revision in one slot",
            )
        attribute_keys = tuple(item.key for item in self.catalog_attributes)
        if len(set(attribute_keys)) != len(attribute_keys):
            raise ComponentLockResolutionError(
                "duplicate-catalog-attribute", "catalog attribute keys must be unique"
            )
        asset_ids = tuple(item.selector.asset_id for item in self.assets)
        if len(set(asset_ids)) != len(asset_ids):
            raise ComponentLockResolutionError(
                "duplicate-asset", "a Component cannot repeat an asset ID"
            )
        if any(
            not isinstance(item, LockedFlavorRequirement)
            for item in self.flavor_requirements
        ):
            raise TypeError(
                "resolved Flavor requirements require LockedFlavorRequirement values"
            )
        requirement_ids = tuple(
            item.requirement_id for item in self.authoring.requires
        ) + tuple(item.requirement.requirement_id for item in self.flavor_requirements)
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ComponentLockResolutionError(
                "duplicate-requirement",
                "Component and selected Flavor requirement IDs must be unique",
            )
        selected_flavors = {
            item.flavor_revision.uri
            for item in self.flavor_candidates
            if item.status is CandidateStatus.SELECTED
        }
        if any(
            item.flavor_revision.identity.uri not in selected_flavors
            for item in self.flavor_requirements
        ):
            raise ComponentLockResolutionError(
                "unselected-flavor-requirement",
                "Flavor requirements must originate from an exact selected Flavor",
            )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "authoring": self.authoring.to_dict(),
            "specifications": [item.to_dict() for item in self.specifications],
            "authoring_inputs": [
                item.to_dict()
                for item in sorted(
                    self.authoring_inputs, key=lambda item: (item.kind, item.uri)
                )
            ],
            "workflow_definition": self.workflow_definition.to_dict(),
            "routing_policy": self.routing_policy.to_dict(),
            "acceptance_contracts": [
                item.to_dict()
                for item in sorted(
                    self.acceptance_contracts, key=lambda item: (item.kind, item.uri)
                )
            ],
            "repository_sources": [
                item.to_dict()
                for item in sorted(
                    self.repository_sources,
                    key=lambda item: item.dependency.dependency_id,
                )
            ],
            "public_interfaces": [
                item.to_dict()
                for item in sorted(
                    self.public_interfaces, key=lambda item: (item.kind, item.uri)
                )
            ],
            "flavor_candidates": [
                item.to_dict()
                for item in sorted(
                    self.flavor_candidates,
                    key=lambda item: (item.slot_id, item.flavor_revision.uri),
                )
            ],
            "catalog_attributes": [
                item.to_dict()
                for item in sorted(self.catalog_attributes, key=lambda item: item.key)
            ],
        }
        if self.assets:
            value["assets"] = [item.to_dict() for item in self.assets]
        if self.flavor_requirements:
            value["flavor_requirements"] = [
                item.to_dict()
                for item in sorted(
                    self.flavor_requirements,
                    key=lambda item: item.requirement.requirement_id,
                )
            ]
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ResolvedComponentNodeInput"
    ) -> ResolvedComponentNodeInput:
        data = _wire_fields(
            value,
            path=path,
            required=frozenset(
                {
                    "authoring",
                    "specifications",
                    "authoring_inputs",
                    "workflow_definition",
                    "routing_policy",
                    "acceptance_contracts",
                    "repository_sources",
                    "public_interfaces",
                    "flavor_candidates",
                    "catalog_attributes",
                }
            ),
            optional=frozenset({"assets", "flavor_requirements"}),
        )
        try:
            authoring = ComponentAuthoring.from_dict(
                data["authoring"], path=f"{path}.authoring"
            )
            specifications = tuple(
                ContentReference.from_dict(item, path=f"{path}.specifications[{index}]")
                for index, item in enumerate(
                    _wire_list(data["specifications"], path=f"{path}.specifications")
                )
            )
            authoring_inputs = tuple(
                ContentReference.from_dict(
                    item, path=f"{path}.authoring_inputs[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data["authoring_inputs"], path=f"{path}.authoring_inputs"
                    )
                )
            )
            workflow = ContentReference.from_dict(
                data["workflow_definition"], path=f"{path}.workflow_definition"
            )
            routing = ContentReference.from_dict(
                data["routing_policy"], path=f"{path}.routing_policy"
            )
            acceptance = tuple(
                ContentReference.from_dict(
                    item, path=f"{path}.acceptance_contracts[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data["acceptance_contracts"],
                        path=f"{path}.acceptance_contracts",
                    )
                )
            )
            repositories = tuple(
                RepositorySourceLock.from_dict(
                    item, path=f"{path}.repository_sources[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data["repository_sources"], path=f"{path}.repository_sources"
                    )
                )
            )
            interfaces = tuple(
                ContentReference.from_dict(
                    item, path=f"{path}.public_interfaces[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data["public_interfaces"], path=f"{path}.public_interfaces"
                    )
                )
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ComponentLockResolutionError):
                raise
            raise ComponentLockResolutionError(
                "invalid-resolution-plan-wire",
                f"{path} contains an invalid exact contract: {exc}",
            ) from exc
        return cls(
            authoring=authoring,
            specifications=specifications,
            authoring_inputs=authoring_inputs,
            workflow_definition=workflow,
            routing_policy=routing,
            acceptance_contracts=acceptance,
            repository_sources=repositories,
            public_interfaces=interfaces,
            flavor_candidates=tuple(
                FlavorCandidateInput.from_dict(
                    item, path=f"{path}.flavor_candidates[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data["flavor_candidates"], path=f"{path}.flavor_candidates"
                    )
                )
            ),
            catalog_attributes=tuple(
                CatalogAttribute.from_dict(
                    item, path=f"{path}.catalog_attributes[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data["catalog_attributes"], path=f"{path}.catalog_attributes"
                    )
                )
            ),
            assets=tuple(
                ResolvedComponentAsset.from_dict(item, path=f"{path}.assets[{index}]")
                for index, item in enumerate(
                    _wire_list(data.get("assets", []), path=f"{path}.assets")
                )
            ),
            flavor_requirements=tuple(
                LockedFlavorRequirement.from_dict(
                    item, path=f"{path}.flavor_requirements[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data.get("flavor_requirements", []),
                        path=f"{path}.flavor_requirements",
                    )
                )
            ),
        )


def _node_requirements(
    node: ResolvedComponentNodeInput,
) -> tuple[CapabilityRequirement, ...]:
    return (
        *node.authoring.requires,
        *(item.requirement for item in node.flavor_requirements),
    )


@dataclass(frozen=True, slots=True)
class RequirementProviderInput:
    """Explicit selected provider for one authored consumer requirement."""

    consumer_authoring_identity: ContentIdentity
    requirement_id: str
    provider_authoring_identity: ContentIdentity

    def __post_init__(self) -> None:
        if not all(
            isinstance(item, ContentIdentity)
            for item in (
                self.consumer_authoring_identity,
                self.provider_authoring_identity,
            )
        ):
            raise TypeError("requirement providers require typed authoring identities")
        if not self.requirement_id:
            raise ComponentLockResolutionError(
                "invalid-requirement-selection", "requirement ID cannot be empty"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "consumer_authoring_identity": self.consumer_authoring_identity.to_dict(),
            "requirement_id": self.requirement_id,
            "provider_authoring_identity": self.provider_authoring_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RequirementProviderInput"
    ) -> RequirementProviderInput:
        data = _wire_fields(
            value,
            path=path,
            required=frozenset(
                {
                    "consumer_authoring_identity",
                    "requirement_id",
                    "provider_authoring_identity",
                }
            ),
        )
        return cls(
            consumer_authoring_identity=_wire_identity(
                data["consumer_authoring_identity"],
                path=f"{path}.consumer_authoring_identity",
            ),
            requirement_id=_wire_string(
                data["requirement_id"], path=f"{path}.requirement_id"
            ),
            provider_authoring_identity=_wire_identity(
                data["provider_authoring_identity"],
                path=f"{path}.provider_authoring_identity",
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentLockResolutionPlan:
    """Complete exact inputs whose identity can be pinned before resolution."""

    target_name: str
    target_profile_identity: ContentIdentity
    selection_policy_identity: ContentIdentity
    resolver_identity: ContentIdentity
    catalog_identity: ContentIdentity
    root_authoring_identity: ContentIdentity
    nodes: tuple[ResolvedComponentNodeInput, ...]
    requirement_providers: tuple[RequirementProviderInput, ...]
    provider_resolutions: tuple[ProviderResolution, ...] = ()

    def __post_init__(self) -> None:
        if not self.target_name:
            raise ComponentLockResolutionError(
                "invalid-target", "target name cannot be empty"
            )
        identities = (
            self.target_profile_identity,
            self.selection_policy_identity,
            self.resolver_identity,
            self.catalog_identity,
            self.root_authoring_identity,
        )
        if any(not isinstance(item, ContentIdentity) for item in identities):
            raise TypeError("resolution plan identities must be ContentIdentity values")
        node_keys = tuple(item.authoring.identity.uri for item in self.nodes)
        if not node_keys or len(set(node_keys)) != len(node_keys):
            raise ComponentLockResolutionError(
                "duplicate-component-input",
                "resolution requires unique non-empty Component node inputs",
            )
        requirement_keys = tuple(
            (item.consumer_authoring_identity.uri, item.requirement_id)
            for item in self.requirement_providers
        )
        if len(set(requirement_keys)) != len(requirement_keys):
            raise ComponentLockResolutionError(
                "duplicate-requirement-selection",
                "one consumer requirement cannot select multiple providers",
            )
        if any(
            not isinstance(item, ProviderResolution)
            for item in self.provider_resolutions
        ):
            raise TypeError(
                "provider resolutions must contain ProviderResolution values"
            )
        provider_resolution_ids = tuple(
            item.request.resolution_id for item in self.provider_resolutions
        )
        if provider_resolution_ids != tuple(sorted(set(provider_resolution_ids))):
            raise ComponentLockResolutionError(
                "duplicate-provider-resolution",
                "provider resolutions require unique canonical resolution IDs",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": _PLAN_SCHEMA,
            "target_name": self.target_name,
            "target_profile_identity": self.target_profile_identity.to_dict(),
            "selection_policy_identity": self.selection_policy_identity.to_dict(),
            "resolver_identity": self.resolver_identity.to_dict(),
            "catalog_identity": self.catalog_identity.to_dict(),
            "root_authoring_identity": self.root_authoring_identity.to_dict(),
            "nodes": [
                item.to_dict()
                for item in sorted(
                    self.nodes, key=lambda item: item.authoring.identity.uri
                )
            ],
            "requirement_providers": [
                item.to_dict()
                for item in sorted(
                    self.requirement_providers,
                    key=lambda item: (
                        item.consumer_authoring_identity.uri,
                        item.requirement_id,
                    ),
                )
            ],
        }
        if self.provider_resolutions:
            value["provider_resolutions"] = [
                item.to_dict() for item in self.provider_resolutions
            ]
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentLockResolutionPlan"
    ) -> ComponentLockResolutionPlan:
        data = _wire_fields(
            value,
            path=path,
            schema=_PLAN_SCHEMA,
            required=frozenset(
                {
                    "target_name",
                    "target_profile_identity",
                    "selection_policy_identity",
                    "resolver_identity",
                    "catalog_identity",
                    "root_authoring_identity",
                    "nodes",
                    "requirement_providers",
                }
            ),
            optional=frozenset({"provider_resolutions"}),
        )
        return cls(
            target_name=_wire_string(data["target_name"], path=f"{path}.target_name"),
            target_profile_identity=_wire_identity(
                data["target_profile_identity"],
                path=f"{path}.target_profile_identity",
            ),
            selection_policy_identity=_wire_identity(
                data["selection_policy_identity"],
                path=f"{path}.selection_policy_identity",
            ),
            resolver_identity=_wire_identity(
                data["resolver_identity"], path=f"{path}.resolver_identity"
            ),
            catalog_identity=_wire_identity(
                data["catalog_identity"], path=f"{path}.catalog_identity"
            ),
            root_authoring_identity=_wire_identity(
                data["root_authoring_identity"],
                path=f"{path}.root_authoring_identity",
            ),
            nodes=tuple(
                ResolvedComponentNodeInput.from_dict(
                    item, path=f"{path}.nodes[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(data["nodes"], path=f"{path}.nodes")
                )
            ),
            requirement_providers=tuple(
                RequirementProviderInput.from_dict(
                    item, path=f"{path}.requirement_providers[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data["requirement_providers"],
                        path=f"{path}.requirement_providers",
                    )
                )
            ),
            provider_resolutions=tuple(
                ProviderResolution.from_dict(
                    item, path=f"{path}.provider_resolutions[{index}]"
                )
                for index, item in enumerate(
                    _wire_list(
                        data.get("provider_resolutions", []),
                        path=f"{path}.provider_resolutions",
                    )
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentLockResolutionResult:
    lock: ComponentLock
    catalog_audit: ComponentResolutionAudit
    input_evidence_identity: ContentIdentity


class ComponentLockResolver:
    """Assemble one canonical selected graph without consulting mutable state."""

    def resolve(
        self,
        plan: ComponentLockResolutionPlan,
        *,
        expected_input_evidence_identity: ContentIdentity,
    ) -> ComponentLockResolutionResult:
        if not isinstance(plan, ComponentLockResolutionPlan):
            raise TypeError("Component lock resolution requires a typed plan")
        if not isinstance(expected_input_evidence_identity, ContentIdentity):
            raise TypeError("expected input evidence must be a ContentIdentity")
        input_identity = plan.identity
        if input_identity != expected_input_evidence_identity:
            raise ComponentLockResolutionError(
                "stale-resolution-input",
                "exact Component/catalog inputs changed after planning",
            )

        ordered_inputs = tuple(
            sorted(plan.nodes, key=lambda item: item.authoring.identity.uri)
        )
        nodes = tuple(self._node(item, plan) for item in ordered_inputs)
        by_authoring = {
            item.authoring.identity.uri: (item, node)
            for item, node in zip(ordered_inputs, nodes, strict=True)
        }
        if plan.root_authoring_identity.uri not in by_authoring:
            raise ComponentLockResolutionError(
                "unknown-root-component", "root authoring is absent from exact inputs"
            )

        edges: list[ExecutableComponentEdge] = []
        satisfactions: dict[str, list[RequirementConstraintSatisfaction]] = {
            node.revision.identity.uri: [] for node in nodes
        }
        for selected in sorted(
            plan.requirement_providers,
            key=lambda item: (
                item.consumer_authoring_identity.uri,
                item.requirement_id,
            ),
        ):
            consumer = by_authoring.get(selected.consumer_authoring_identity.uri)
            provider = by_authoring.get(selected.provider_authoring_identity.uri)
            if consumer is None or provider is None:
                raise ComponentLockResolutionError(
                    "unknown-requirement-node",
                    "requirement selection references a Component outside exact inputs",
                )
            consumer_input, consumer_node = consumer
            provider_input, provider_node = provider
            requirement = next(
                (
                    item
                    for item in _node_requirements(consumer_input)
                    if item.requirement_id == selected.requirement_id
                ),
                None,
            )
            if requirement is None:
                raise ComponentLockResolutionError(
                    "unknown-requirement",
                    "requirement selection is absent from Component or selected "
                    "Flavor authority",
                )
            provided = next(
                (
                    item
                    for item in provider_input.authoring.provides
                    if item.name == requirement.capability
                ),
                None,
            )
            if provided is None:
                raise ComponentLockResolutionError(
                    "capability-mismatch",
                    "selected provider does not declare the required capability",
                )
            self._require_constraints(requirement.constraints, provider_input)
            interface_identity = None
            if requirement.dependency_kind is DependencyKind.GENERATION:
                bindings = tuple(
                    item
                    for item in provider_node.interface_bindings
                    if item.capability == requirement.capability
                )
                if len(bindings) != 1:
                    raise ComponentLockResolutionError(
                        "generation-interface-missing",
                        "generation provider must expose one exact public interface",
                    )
                interface_identity = bindings[0].interface_identity
            edges.append(
                ExecutableComponentEdge(
                    consumer_revision=consumer_node.revision.identity,
                    provider_revision=provider_node.revision.identity,
                    requirement_id=requirement.requirement_id,
                    capability=requirement.capability,
                    kind=requirement.dependency_kind,
                    public_interface_identity=interface_identity,
                    optional=requirement.optional,
                )
            )
            attributes = tuple(
                sorted(provider_input.catalog_attributes, key=lambda item: item.key)
            )
            evidence = canonical_identity(
                {
                    "schema": "literate-ai/constraint-satisfaction-evidence@1",
                    "consumer_authoring_identity": (
                        consumer_input.authoring.identity.uri
                    ),
                    "provider_authoring_identity": (
                        provider_input.authoring.identity.uri
                    ),
                    "requirement": requirement.to_dict(),
                    "provided_capability": provided.to_dict(),
                    "provider_catalog_attributes": [
                        item.to_dict() for item in attributes
                    ],
                    "target_flavor_selection_identity": (
                        consumer_node.target_flavor_selection.identity.uri
                    ),
                }
            )
            satisfactions[consumer_node.revision.identity.uri].append(
                RequirementConstraintSatisfaction(
                    consumer_revision=consumer_node.revision.identity,
                    provider_revision=provider_node.revision.identity,
                    requirement_id=requirement.requirement_id,
                    constraints=requirement.constraints,
                    target_name=plan.target_name,
                    target_profile_identity=plan.target_profile_identity,
                    selection_policy_identity=plan.selection_policy_identity,
                    target_flavor_selection_identity=(
                        consumer_node.target_flavor_selection.identity
                    ),
                    satisfaction_evidence_identity=evidence,
                )
            )

        nodes = tuple(
            replace(
                node,
                requirement_constraint_satisfactions=tuple(
                    sorted(
                        satisfactions[node.revision.identity.uri],
                        key=lambda item: item.requirement_id,
                    )
                ),
            )
            for node in nodes
        )
        root_revision = by_authoring[plan.root_authoring_identity.uri][
            1
        ].revision.identity
        lock = ComponentLock(
            target_name=plan.target_name,
            target_profile_identity=plan.target_profile_identity,
            selection_policy_identity=plan.selection_policy_identity,
            resolver_identity=plan.resolver_identity,
            root_revision=root_revision,
            nodes=tuple(sorted(nodes, key=lambda item: item.revision.identity.uri)),
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
                sorted(
                    (item.authoring for item in ordered_inputs),
                    key=lambda item: item.identity.uri,
                )
            ),
            provider_resolutions=plan.provider_resolutions,
        )
        candidate_audit = tuple(
            sorted(
                (
                    NodeFlavorCandidateAudit(
                        component_revision=node.revision.identity,
                        slot_id=candidate.slot_id,
                        flavor_revision=candidate.flavor_revision,
                        status=candidate.status,
                        reasons=tuple(sorted(candidate.reasons)),
                    )
                    for node_input, node in zip(ordered_inputs, nodes, strict=True)
                    for candidate in node_input.flavor_candidates
                ),
                key=lambda item: (
                    item.component_revision.uri,
                    item.slot_id,
                    item.flavor_revision.uri,
                ),
            )
        )
        audit = ComponentResolutionAudit(
            component_lock_identity=lock.identity,
            catalog_identity=plan.catalog_identity,
            resolver_identity=plan.resolver_identity,
            candidates=candidate_audit,
        )
        return ComponentLockResolutionResult(lock, audit, input_identity)

    @staticmethod
    def _node(
        node_input: ResolvedComponentNodeInput,
        plan: ComponentLockResolutionPlan,
    ) -> ComponentLockNode:
        slots_by_id = {item.slot_id: item for item in node_input.authoring.flavor_slots}
        unknown = sorted(
            {item.slot_id for item in node_input.flavor_candidates} - set(slots_by_id)
        )
        if unknown:
            raise ComponentLockResolutionError(
                "unknown-flavor-slot",
                "Flavor candidates reference undeclared slots: " + ", ".join(unknown),
            )
        slot_resolutions = tuple(
            NodeFlavorSlotResolution(
                slot=slot,
                selected=tuple(
                    sorted(
                        (
                            SelectedNodeFlavor(item.value, item.flavor_revision)
                            for item in node_input.flavor_candidates
                            if item.slot_id == slot.slot_id
                            and item.status is CandidateStatus.SELECTED
                        ),
                        key=lambda item: (item.value, item.flavor_revision.uri),
                    )
                ),
            )
            for slot in node_input.authoring.flavor_slots
        )
        selected_revisions = {
            item.flavor_revision.uri: item.flavor_revision
            for slot in slot_resolutions
            for item in slot.selected
        }
        specifications = tuple(node_input.specifications)
        revision = LockedComponentRevision(
            coordinate=node_input.authoring.coordinate,
            version=node_input.authoring.version,
            authoring_identity=node_input.authoring.identity,
            specification_set_identity=ordered_specification_set_identity(
                node_input.authoring.specification_provider, specifications
            ),
            specifications=specifications,
            selected_flavor_revisions=tuple(
                sorted(selected_revisions.values(), key=lambda item: item.uri)
            ),
            authoring_inputs=tuple(
                sorted(
                    node_input.authoring_inputs, key=lambda item: (item.kind, item.uri)
                )
            ),
            workflow_definition=node_input.workflow_definition,
            routing_policy=node_input.routing_policy,
            acceptance_contracts=tuple(
                sorted(
                    node_input.acceptance_contracts,
                    key=lambda item: (item.kind, item.uri),
                )
            ),
            repository_sources=tuple(
                sorted(
                    node_input.repository_sources,
                    key=lambda item: item.dependency.dependency_id,
                )
            ),
            public_interfaces=tuple(
                sorted(
                    node_input.public_interfaces,
                    key=lambda item: (item.kind, item.uri),
                )
            ),
            definition=node_input.authoring,
            assets=tuple(
                sorted(node_input.assets, key=lambda item: item.selector.asset_id)
            ),
            flavor_requirements=tuple(
                sorted(
                    node_input.flavor_requirements,
                    key=lambda item: item.requirement.requirement_id,
                )
            ),
        )
        target_selection = NodeTargetFlavorSelection(
            component_revision=revision.identity,
            target_name=plan.target_name,
            target_profile_identity=plan.target_profile_identity,
            selection_policy_identity=plan.selection_policy_identity,
            slots=slot_resolutions,
        )
        references = {item.uri: item for item in node_input.public_interfaces}
        bindings = tuple(
            sorted(
                (
                    ComponentInterfaceBinding(
                        revision.identity,
                        provided.name,
                        references[provided.interface.uri].identity,
                    )
                    for provided in node_input.authoring.provides
                    if provided.interface is not None
                    and provided.interface.uri in references
                ),
                key=lambda item: item.capability,
            )
        )
        return ComponentLockNode(revision, target_selection, bindings)

    @staticmethod
    def _require_constraints(
        constraints: tuple[CapabilityConstraint, ...],
        provider: ResolvedComponentNodeInput,
    ) -> None:
        attributes = {
            item.key: set(item.values) for item in provider.catalog_attributes
        }
        for constraint in constraints:
            actual = attributes.get(constraint.key)
            if actual is None:
                raise ComponentLockResolutionError(
                    "constraint-unsatisfied",
                    f"provider lacks constraint attribute {constraint.key!r}",
                )
            expected = set(constraint.values)
            if constraint.operator in {"in", "equals"}:
                accepted = bool(actual & expected)
            elif constraint.operator == "not-in":
                accepted = not bool(actual & expected)
            elif constraint.operator == "contains-all":
                accepted = expected <= actual
            else:
                raise ComponentLockResolutionError(
                    "unsupported-constraint",
                    f"unsupported constraint operator {constraint.operator!r}",
                )
            if not accepted:
                raise ComponentLockResolutionError(
                    "constraint-unsatisfied",
                    f"provider does not satisfy constraint {constraint.key!r}",
                )


__all__ = [
    "CatalogAttribute",
    "ComponentLockResolutionError",
    "ComponentLockResolutionPlan",
    "ComponentLockResolutionResult",
    "ComponentLockResolver",
    "FlavorCandidateInput",
    "RequirementProviderInput",
    "ResolvedComponentNodeInput",
]
