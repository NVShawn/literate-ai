"""Selected-only Component locks and separate catalog-resolution audit evidence."""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    parse_tuple,
    string_tuple,
    unique,
)
from ..blobs import BlobRef
from ..capabilities import CapabilityConstraint, CapabilityRequirement
from ..executable_components import (
    ComponentInterfaceBinding,
    ExecutableComponentEdge,
    NodeTargetFlavorSelection,
)
from ..executable_components._common import (
    canonical_identities,
    identity,
    portable_name,
    texts,
    tuple_value,
)
from ..flavors import CandidateStatus, FlavorRevision
from ..identity import (
    ComponentCoordinate,
    ContentIdentity,
    ContentReference,
    contract_identity,
)
from ..provider_resolution import ProviderResolution
from ..repositories import RepositorySourceLock
from ..versioning import SemanticVersion, semantic_version
from .authoring import (
    AuthoredProvidedCapability,
    ComponentAssetSelector,
    ComponentAuthoring,
    ComponentContentSelector,
)

LOCKED_COMPONENT_REVISION_SCHEMA = "urn:literate-ai:schema:v2:locked-component-revision"
LOCKED_FLAVOR_REQUIREMENT_SCHEMA = "urn:literate-ai:schema:v2:locked-flavor-requirement"
RESOLVED_COMPONENT_ASSET_SCHEMA = "urn:literate-ai:schema:v2:resolved-component-asset"
COMPONENT_LOCK_NODE_SCHEMA = "urn:literate-ai:schema:v2:component-lock-node"
COMPONENT_LOCK_SCHEMA = "urn:literate-ai:schema:v2:component-lock"
COMPONENT_RESOLUTION_AUDIT_SCHEMA = (
    "urn:literate-ai:schema:v2:component-resolution-audit"
)
_VERSION_TERM = re.compile(r"^(>=|<=|>|<|==|=|\^|~)?\s*(.+)$")


def _canonical_values(
    values: object,
    *,
    path: str,
    expected_type: type[Any],
    key: Any,
    label: str,
    maximum: int = 256,
) -> tuple[Any, ...]:
    items = tuple_value(values, path)
    if len(items) > maximum or any(
        not isinstance(item, expected_type) for item in items
    ):
        fail(path, f"must contain at most {maximum} {label} values")
    keys = tuple(key(item) for item in items)
    unique(keys, path, label)
    if keys != tuple(sorted(keys)):
        fail(path, f"must use canonical {label} order")
    return items


def _reference_key(reference: ContentReference) -> tuple[str, str]:
    return reference.kind, reference.uri


def _ordered_specifications(
    values: object, *, path: str
) -> tuple[ContentReference, ...]:
    items = tuple_value(values, path)
    if (
        not items
        or len(items) > 256
        or any(not isinstance(item, ContentReference) for item in items)
    ):
        fail(path, "must contain between 1 and 256 specification references")
    if any(item.kind != "specification" for item in items):
        fail(path, "must contain only specification references")
    unique(tuple(item.uri for item in items), path, "specification URIs")
    return items


def ordered_specification_set_identity(
    provider: str, specifications: tuple[ContentReference, ...]
) -> ContentIdentity:
    """Bind the provider and authored root order to exact specification bytes."""

    texts((provider,), "ordered_specification_set_identity.provider", required=True)
    selected = _ordered_specifications(
        specifications, path="ordered_specification_set_identity.specifications"
    )
    return contract_identity(
        _OrderedSpecificationSet(provider=provider, specifications=selected)
    )


@dataclass(frozen=True, slots=True)
class _OrderedSpecificationSet:
    provider: str
    specifications: tuple[ContentReference, ...]

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": "literate-ai/ordered-specification-set@1",
            "provider": self.provider,
            "specifications": [item.to_dict() for item in self.specifications],
        }
        return value


@dataclass(frozen=True, slots=True)
class ResolvedComponentAsset:
    """An authored asset selector locked to exact reachable bytes."""

    selector: ComponentAssetSelector
    blob: BlobRef

    SCHEMA: ClassVar[str] = RESOLVED_COMPONENT_ASSET_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.selector, ComponentAssetSelector):
            fail("ResolvedComponentAsset.selector", "must be a ComponentAssetSelector")
        if not isinstance(self.blob, BlobRef):
            fail("ResolvedComponentAsset.blob", "must be a BlobRef")
        if (
            self.selector.pin is not None
            and self.selector.pin.uri != self.blob.identity
        ):
            fail("ResolvedComponentAsset.blob", "does not match the authored pin")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "selector": self.selector.to_dict(),
            "blob": self.blob.to_dict(),
        }
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ResolvedComponentAsset"
    ) -> ResolvedComponentAsset:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"selector", "blob"}),
        )
        return cls(
            ComponentAssetSelector.from_dict(data["selector"], path=f"{path}.selector"),
            BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
        )


@dataclass(frozen=True, slots=True)
class LockedFlavorRequirement:
    """One exact requirement contributed by one selected Flavor revision."""

    flavor_revision: FlavorRevision
    requirement: CapabilityRequirement

    SCHEMA: ClassVar[str] = LOCKED_FLAVOR_REQUIREMENT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.flavor_revision, FlavorRevision):
            fail(
                "LockedFlavorRequirement.flavor_revision",
                "must be a FlavorRevision",
            )
        if not isinstance(self.requirement, CapabilityRequirement):
            fail(
                "LockedFlavorRequirement.requirement",
                "must be a CapabilityRequirement",
            )
        if self.requirement not in self.flavor_revision.definition.requires:
            fail(
                "LockedFlavorRequirement.requirement",
                "must be declared by the exact Flavor revision",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "flavor_revision": self.flavor_revision.to_dict(),
            "requirement": self.requirement.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LockedFlavorRequirement"
    ) -> LockedFlavorRequirement:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"flavor_revision", "requirement"}),
        )
        return cls(
            FlavorRevision.from_dict(
                data["flavor_revision"], path=f"{path}.flavor_revision"
            ),
            CapabilityRequirement.from_dict(
                data["requirement"], path=f"{path}.requirement"
            ),
        )


@dataclass(frozen=True, slots=True)
class LockedComponentRevision:
    """Exact local generation authority, excluding runtime and provenance evidence."""

    coordinate: ComponentCoordinate
    version: str
    authoring_identity: ContentIdentity
    specification_set_identity: ContentIdentity
    specifications: tuple[ContentReference, ...]
    selected_flavor_revisions: tuple[ContentIdentity, ...]
    authoring_inputs: tuple[ContentReference, ...]
    workflow_definition: ContentReference
    routing_policy: ContentReference
    acceptance_contracts: tuple[ContentReference, ...]
    repository_sources: tuple[RepositorySourceLock, ...]
    public_interfaces: tuple[ContentReference, ...]
    definition: ComponentAuthoring = field(repr=False, compare=False)
    assets: tuple[ResolvedComponentAsset, ...] = ()
    flavor_requirements: tuple[LockedFlavorRequirement, ...] = ()

    SCHEMA: ClassVar[str] = LOCKED_COMPONENT_REVISION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate, ComponentCoordinate):
            fail("LockedComponentRevision.coordinate", "must be a ComponentCoordinate")
        semantic_version(self.version, "LockedComponentRevision.version")
        identity(self.authoring_identity, "LockedComponentRevision.authoring_identity")
        if not isinstance(self.definition, ComponentAuthoring):
            fail(
                "LockedComponentRevision.definition",
                "must be a ComponentAuthoring",
            )
        if self.definition.identity != self.authoring_identity:
            fail(
                "LockedComponentRevision.definition",
                "must bind the exact authoring identity",
            )
        if (
            self.definition.coordinate != self.coordinate
            or self.definition.version != self.version
        ):
            fail(
                "LockedComponentRevision.definition",
                "coordinate and version must match the locked revision",
            )
        identity(
            self.specification_set_identity,
            "LockedComponentRevision.specification_set_identity",
        )
        _ordered_specifications(
            self.specifications, path="LockedComponentRevision.specifications"
        )
        selected_flavors = canonical_identities(
            self.selected_flavor_revisions,
            "LockedComponentRevision.selected_flavor_revisions",
        )
        if len(selected_flavors) > 256:
            fail(
                "LockedComponentRevision.selected_flavor_revisions",
                "must contain at most 256 identities",
            )
        _canonical_values(
            self.authoring_inputs,
            path="LockedComponentRevision.authoring_inputs",
            expected_type=ContentReference,
            key=_reference_key,
            label="resolved authoring references",
        )
        for field_name in ("workflow_definition", "routing_policy"):
            if not isinstance(getattr(self, field_name), ContentReference):
                fail(
                    f"LockedComponentRevision.{field_name}",
                    "must be a ContentReference",
                )
        if self.workflow_definition.kind != "workflow":
            fail(
                "LockedComponentRevision.workflow_definition.kind",
                "must be 'workflow'",
            )
        if self.routing_policy.kind != "routing-policy":
            fail(
                "LockedComponentRevision.routing_policy.kind",
                "must be 'routing-policy'",
            )
        _canonical_values(
            self.acceptance_contracts,
            path="LockedComponentRevision.acceptance_contracts",
            expected_type=ContentReference,
            key=_reference_key,
            label="resolved acceptance references",
        )
        if any(
            item.kind != "acceptance-contract" for item in self.acceptance_contracts
        ):
            fail(
                "LockedComponentRevision.acceptance_contracts",
                "must contain only acceptance-contract references",
            )
        _canonical_values(
            self.repository_sources,
            path="LockedComponentRevision.repository_sources",
            expected_type=RepositorySourceLock,
            key=lambda item: item.dependency.dependency_id,
            label="repository source dependency IDs",
        )
        _canonical_values(
            self.public_interfaces,
            path="LockedComponentRevision.public_interfaces",
            expected_type=ContentReference,
            key=_reference_key,
            label="public interface references",
        )
        if any(
            item.kind != "public-interface-contract" for item in self.public_interfaces
        ):
            fail(
                "LockedComponentRevision.public_interfaces",
                "must contain only public-interface-contract references",
            )
        _canonical_values(
            self.assets,
            path="LockedComponentRevision.assets",
            expected_type=ResolvedComponentAsset,
            key=lambda item: item.selector.asset_id,
            label="asset IDs",
        )
        flavor_requirements = _canonical_values(
            self.flavor_requirements,
            path="LockedComponentRevision.flavor_requirements",
            expected_type=LockedFlavorRequirement,
            key=lambda item: item.requirement.requirement_id,
            label="selected Flavor requirement IDs",
        )
        if any(
            item.flavor_revision.identity not in selected_flavors
            for item in flavor_requirements
        ):
            fail(
                "LockedComponentRevision.flavor_requirements",
                "must originate from an exact selected Flavor revision",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "coordinate": self.coordinate.to_dict(),
            "version": self.version,
            "authoring_identity": self.authoring_identity.to_dict(),
            "specification_set_identity": self.specification_set_identity.to_dict(),
            "specifications": [item.to_dict() for item in self.specifications],
            "selected_flavor_revisions": [
                item.to_dict() for item in self.selected_flavor_revisions
            ],
            "authoring_inputs": [item.to_dict() for item in self.authoring_inputs],
            "workflow_definition": self.workflow_definition.to_dict(),
            "routing_policy": self.routing_policy.to_dict(),
            "acceptance_contracts": [
                item.to_dict() for item in self.acceptance_contracts
            ],
            "repository_sources": [item.to_dict() for item in self.repository_sources],
            "public_interfaces": [item.to_dict() for item in self.public_interfaces],
        }
        if self.assets:
            value["assets"] = [item.to_dict() for item in self.assets]
        if self.flavor_requirements:
            value["flavor_requirements"] = [
                item.to_dict() for item in self.flavor_requirements
            ]
        return value

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        definitions: tuple[ComponentAuthoring, ...],
        path: str = "LockedComponentRevision",
    ) -> LockedComponentRevision:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "coordinate",
                    "version",
                    "authoring_identity",
                    "specification_set_identity",
                    "specifications",
                    "selected_flavor_revisions",
                    "authoring_inputs",
                    "workflow_definition",
                    "routing_policy",
                    "acceptance_contracts",
                    "repository_sources",
                    "public_interfaces",
                }
            ),
            optional=frozenset({"assets", "flavor_requirements"}),
        )
        authoring_identity = ContentIdentity.from_dict(
            data["authoring_identity"], path=f"{path}.authoring_identity"
        )
        matches = tuple(
            definition
            for definition in definitions
            if definition.identity == authoring_identity
        )
        if len(matches) != 1:
            fail(
                f"{path}.definition",
                "requires exactly one external ComponentAuthoring matching "
                "authoring_identity",
            )
        return cls(
            coordinate=ComponentCoordinate.from_dict(
                data["coordinate"], path=f"{path}.coordinate"
            ),
            version=semantic_version(data["version"], f"{path}.version"),
            authoring_identity=authoring_identity,
            specification_set_identity=ContentIdentity.from_dict(
                data["specification_set_identity"],
                path=f"{path}.specification_set_identity",
            ),
            specifications=parse_tuple(
                data["specifications"],
                f"{path}.specifications",
                ContentReference.from_dict,
            ),
            selected_flavor_revisions=parse_tuple(
                data["selected_flavor_revisions"],
                f"{path}.selected_flavor_revisions",
                ContentIdentity.from_dict,
            ),
            authoring_inputs=parse_tuple(
                data["authoring_inputs"],
                f"{path}.authoring_inputs",
                ContentReference.from_dict,
            ),
            workflow_definition=ContentReference.from_dict(
                data["workflow_definition"], path=f"{path}.workflow_definition"
            ),
            routing_policy=ContentReference.from_dict(
                data["routing_policy"], path=f"{path}.routing_policy"
            ),
            acceptance_contracts=parse_tuple(
                data["acceptance_contracts"],
                f"{path}.acceptance_contracts",
                ContentReference.from_dict,
            ),
            repository_sources=parse_tuple(
                data["repository_sources"],
                f"{path}.repository_sources",
                RepositorySourceLock.from_dict,
            ),
            public_interfaces=parse_tuple(
                data["public_interfaces"],
                f"{path}.public_interfaces",
                ContentReference.from_dict,
            ),
            definition=matches[0],
            assets=parse_tuple(
                data.get("assets", []),
                f"{path}.assets",
                ResolvedComponentAsset.from_dict,
            ),
            flavor_requirements=parse_tuple(
                data.get("flavor_requirements", []),
                f"{path}.flavor_requirements",
                LockedFlavorRequirement.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class RequirementConstraintSatisfaction:
    """Exact resolver decision for one selected requirement and target context."""

    consumer_revision: ContentIdentity
    provider_revision: ContentIdentity
    requirement_id: str
    constraints: tuple[CapabilityConstraint, ...]
    target_name: str
    target_profile_identity: ContentIdentity
    selection_policy_identity: ContentIdentity
    target_flavor_selection_identity: ContentIdentity
    satisfaction_evidence_identity: ContentIdentity

    SCHEMA: ClassVar[str] = "literate-ai/requirement-constraint-satisfaction@1"

    def __post_init__(self) -> None:
        identity(
            self.consumer_revision,
            "RequirementConstraintSatisfaction.consumer_revision",
        )
        identity(
            self.provider_revision,
            "RequirementConstraintSatisfaction.provider_revision",
        )
        if self.consumer_revision == self.provider_revision:
            fail(
                "RequirementConstraintSatisfaction.provider_revision",
                "must identify another Component revision",
            )
        portable_name(
            self.requirement_id,
            "RequirementConstraintSatisfaction.requirement_id",
        )
        constraints = tuple_value(
            self.constraints, "RequirementConstraintSatisfaction.constraints"
        )
        if len(constraints) > 256 or any(
            not isinstance(item, CapabilityConstraint) for item in constraints
        ):
            fail(
                "RequirementConstraintSatisfaction.constraints",
                "must contain at most 256 CapabilityConstraint values",
            )
        unique(
            tuple(item.key for item in constraints),
            "RequirementConstraintSatisfaction.constraints",
            "constraint keys",
        )
        portable_name(self.target_name, "RequirementConstraintSatisfaction.target_name")
        identity(
            self.target_profile_identity,
            "RequirementConstraintSatisfaction.target_profile_identity",
        )
        identity(
            self.selection_policy_identity,
            "RequirementConstraintSatisfaction.selection_policy_identity",
        )
        identity(
            self.target_flavor_selection_identity,
            "RequirementConstraintSatisfaction.target_flavor_selection_identity",
        )
        identity(
            self.satisfaction_evidence_identity,
            "RequirementConstraintSatisfaction.satisfaction_evidence_identity",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "consumer_revision": self.consumer_revision.to_dict(),
            "provider_revision": self.provider_revision.to_dict(),
            "requirement_id": self.requirement_id,
            "constraints": [item.to_dict() for item in self.constraints],
            "target_name": self.target_name,
            "target_profile_identity": self.target_profile_identity.to_dict(),
            "selection_policy_identity": self.selection_policy_identity.to_dict(),
            "target_flavor_selection_identity": (
                self.target_flavor_selection_identity.to_dict()
            ),
            "satisfaction_evidence_identity": (
                self.satisfaction_evidence_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "RequirementConstraintSatisfaction",
    ) -> RequirementConstraintSatisfaction:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "consumer_revision",
                    "provider_revision",
                    "requirement_id",
                    "constraints",
                    "target_name",
                    "target_profile_identity",
                    "selection_policy_identity",
                    "target_flavor_selection_identity",
                    "satisfaction_evidence_identity",
                }
            ),
        )
        return cls(
            consumer_revision=ContentIdentity.from_dict(
                data["consumer_revision"], path=f"{path}.consumer_revision"
            ),
            provider_revision=ContentIdentity.from_dict(
                data["provider_revision"], path=f"{path}.provider_revision"
            ),
            requirement_id=portable_name(
                data["requirement_id"], f"{path}.requirement_id"
            ),
            constraints=parse_tuple(
                data["constraints"],
                f"{path}.constraints",
                CapabilityConstraint.from_dict,
            ),
            target_name=portable_name(data["target_name"], f"{path}.target_name"),
            target_profile_identity=ContentIdentity.from_dict(
                data["target_profile_identity"],
                path=f"{path}.target_profile_identity",
            ),
            selection_policy_identity=ContentIdentity.from_dict(
                data["selection_policy_identity"],
                path=f"{path}.selection_policy_identity",
            ),
            target_flavor_selection_identity=ContentIdentity.from_dict(
                data["target_flavor_selection_identity"],
                path=f"{path}.target_flavor_selection_identity",
            ),
            satisfaction_evidence_identity=ContentIdentity.from_dict(
                data["satisfaction_evidence_identity"],
                path=f"{path}.satisfaction_evidence_identity",
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentLockNode:
    """One exact revision, its per-node target selection, and interface bindings."""

    revision: LockedComponentRevision
    target_flavor_selection: NodeTargetFlavorSelection
    interface_bindings: tuple[ComponentInterfaceBinding, ...]
    requirement_constraint_satisfactions: tuple[
        RequirementConstraintSatisfaction, ...
    ] = ()

    SCHEMA: ClassVar[str] = COMPONENT_LOCK_NODE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.revision, LockedComponentRevision):
            fail("ComponentLockNode.revision", "must be a LockedComponentRevision")
        if not isinstance(self.target_flavor_selection, NodeTargetFlavorSelection):
            fail(
                "ComponentLockNode.target_flavor_selection",
                "must be a NodeTargetFlavorSelection",
            )
        revision_identity = self.revision.identity
        if self.target_flavor_selection.component_revision != revision_identity:
            fail(
                "ComponentLockNode.target_flavor_selection.component_revision",
                "must identify the enclosing locked Component revision",
            )
        if (
            self.target_flavor_selection.selected_flavor_revisions
            != self.revision.selected_flavor_revisions
        ):
            fail(
                "ComponentLockNode.target_flavor_selection",
                "must select every and only Flavor revision in the locked revision",
            )
        bindings = _canonical_values(
            self.interface_bindings,
            path="ComponentLockNode.interface_bindings",
            expected_type=ComponentInterfaceBinding,
            key=lambda item: item.capability,
            label="interface capability bindings",
        )
        if any(item.component_revision != revision_identity for item in bindings):
            fail(
                "ComponentLockNode.interface_bindings",
                "must identify the enclosing locked Component revision",
            )
        locked_interfaces = frozenset(
            item.identity.uri for item in self.revision.public_interfaces
        )
        bound_interfaces = frozenset(
            item.interface_identity.uri for item in self.interface_bindings
        )
        if locked_interfaces != bound_interfaces:
            fail(
                "ComponentLockNode.interface_bindings",
                "must bind every and only locked public interface identity",
            )
        satisfactions = _canonical_values(
            self.requirement_constraint_satisfactions,
            path="ComponentLockNode.requirement_constraint_satisfactions",
            expected_type=RequirementConstraintSatisfaction,
            key=lambda item: item.requirement_id,
            label="requirement constraint satisfaction IDs",
        )
        selection = self.target_flavor_selection
        if any(
            item.consumer_revision != revision_identity
            or item.target_name != selection.target_name
            or item.target_profile_identity != selection.target_profile_identity
            or item.selection_policy_identity != selection.selection_policy_identity
            or item.target_flavor_selection_identity != selection.identity
            for item in satisfactions
        ):
            fail(
                "ComponentLockNode.requirement_constraint_satisfactions",
                "must bind the enclosing revision and exact target/Flavor selection",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "revision": self.revision.to_dict(),
            "target_flavor_selection": self.target_flavor_selection.to_dict(),
            "interface_bindings": [item.to_dict() for item in self.interface_bindings],
            "requirement_constraint_satisfactions": [
                item.to_dict() for item in self.requirement_constraint_satisfactions
            ],
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        definitions: tuple[ComponentAuthoring, ...],
        path: str = "ComponentLockNode",
    ) -> ComponentLockNode:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "revision",
                    "target_flavor_selection",
                    "interface_bindings",
                    "requirement_constraint_satisfactions",
                }
            ),
        )
        return cls(
            revision=LockedComponentRevision.from_dict(
                data["revision"],
                definitions=definitions,
                path=f"{path}.revision",
            ),
            target_flavor_selection=NodeTargetFlavorSelection.from_dict(
                data["target_flavor_selection"],
                path=f"{path}.target_flavor_selection",
            ),
            interface_bindings=parse_tuple(
                data["interface_bindings"],
                f"{path}.interface_bindings",
                ComponentInterfaceBinding.from_dict,
            ),
            requirement_constraint_satisfactions=parse_tuple(
                data["requirement_constraint_satisfactions"],
                f"{path}.requirement_constraint_satisfactions",
                RequirementConstraintSatisfaction.from_dict,
            ),
        )


def _require_selector_resolution(
    selector: ComponentContentSelector,
    reference: ContentReference,
    *,
    path: str,
) -> None:
    if selector.kind != reference.kind or selector.uri != reference.uri:
        fail(path, "must resolve the exact authored selector kind and URI")
    if selector.pin is not None and selector.pin != reference.identity:
        fail(path, "must honor the authored selector pin")


def _require_selector_collection(
    selectors: tuple[ComponentContentSelector, ...],
    references: tuple[ContentReference, ...],
    *,
    path: str,
) -> None:
    if len(selectors) != len(references):
        fail(path, "must resolve every and only authored selector")
    resolved = {(item.kind, item.uri): item for item in references}
    if len(resolved) != len(references):
        fail(path, "cannot resolve one selector more than once")
    for selector in selectors:
        reference = resolved.get((selector.kind, selector.uri))
        if reference is None:
            fail(path, "must resolve every and only authored selector")
        _require_selector_resolution(selector, reference, path=path)


def _range_bound(value: str) -> SemanticVersion:
    parts = value.split("-", 1)[0].split("+", 1)[0].split(".")
    if len(parts) < 3 and all(part.isdigit() for part in parts):
        value = value + ".0" * (3 - len(parts))
    return SemanticVersion.parse(value)


def _caret_upper_bound(raw: str, bound: SemanticVersion) -> SemanticVersion:
    precision = len(raw.split("-", 1)[0].split("+", 1)[0].split("."))
    if bound.major > 0 or precision == 1:
        return SemanticVersion(bound.major + 1, 0, 0)
    if bound.minor > 0 or precision == 2:
        return SemanticVersion(0, bound.minor + 1, 0)
    return SemanticVersion(0, 0, bound.patch + 1)


def _version_satisfies(version: str, expression: str) -> bool:
    candidate = SemanticVersion.parse(version)
    if expression.strip() in {"", "*"}:
        return True
    for raw_term in expression.split(","):
        match = _VERSION_TERM.fullmatch(raw_term.strip())
        if match is None:
            raise ValueError(f"unsupported version range term {raw_term!r}")
        operator, raw_bound = match.groups()
        bound = _range_bound(raw_bound)
        operator = operator or "="
        accepted = {
            "=": candidate == bound,
            "==": candidate == bound,
            ">": candidate > bound,
            ">=": candidate >= bound,
            "<": candidate < bound,
            "<=": candidate <= bound,
            "^": candidate >= bound
            and candidate < _caret_upper_bound(raw_bound, bound),
            "~": candidate >= bound
            and candidate < SemanticVersion(bound.major, bound.minor + 1, 0),
        }[operator]
        if not accepted:
            return False
    return True


def _provided_capabilities(
    authoring: ComponentAuthoring,
) -> dict[str, AuthoredProvidedCapability]:
    return {item.name: item for item in authoring.provides}


def _effective_requirements(
    node: ComponentLockNode,
    authoring: ComponentAuthoring,
) -> tuple[CapabilityRequirement, ...]:
    requirements = (
        *authoring.requires,
        *(item.requirement for item in node.revision.flavor_requirements),
    )
    requirement_ids = tuple(item.requirement_id for item in requirements)
    if len(set(requirement_ids)) != len(requirement_ids):
        fail(
            "ComponentLock.nodes",
            "Component and selected Flavor requirement IDs must be unique",
        )
    return requirements


def _require_node_authoring(
    node: ComponentLockNode,
    authoring: ComponentAuthoring,
) -> None:
    revision = node.revision
    path = f"ComponentLock.nodes[{revision.coordinate.uri}]"
    if revision.definition != authoring:
        fail(path, "must retain the exact external ComponentAuthoring definition")
    if revision.authoring_identity != authoring.identity:
        fail(path, "must bind the exact external ComponentAuthoring identity")
    if (
        revision.coordinate != authoring.coordinate
        or revision.version != authoring.version
    ):
        fail(path, "coordinate and version must match exact ComponentAuthoring")
    _effective_requirements(node, authoring)

    roots = tuple(item.uri for item in revision.specifications)
    if roots != authoring.specification_roots:
        fail(
            f"{path}.revision.specifications",
            "must preserve the exact authored specification-root order",
        )
    expected_specification_set = ordered_specification_set_identity(
        authoring.specification_provider, revision.specifications
    )
    if revision.specification_set_identity != expected_specification_set:
        fail(
            f"{path}.revision.specification_set_identity",
            "must bind the exact provider and ordered specification references",
        )

    _require_selector_collection(
        authoring.authoring_inputs,
        revision.authoring_inputs,
        path=f"{path}.revision.authoring_inputs",
    )
    _require_selector_resolution(
        authoring.workflow_definition,
        revision.workflow_definition,
        path=f"{path}.revision.workflow_definition",
    )
    _require_selector_resolution(
        authoring.routing_policy,
        revision.routing_policy,
        path=f"{path}.revision.routing_policy",
    )
    _require_selector_collection(
        authoring.acceptance_contracts,
        revision.acceptance_contracts,
        path=f"{path}.revision.acceptance_contracts",
    )

    node.target_flavor_selection.require_slots(authoring.flavor_slots)

    authored_sources = {
        item.dependency_id: item for item in authoring.source_dependencies
    }
    locked_sources = {
        item.dependency.dependency_id: item for item in revision.repository_sources
    }
    if set(authored_sources) != set(locked_sources):
        fail(
            f"{path}.revision.repository_sources",
            "must lock every and only authored repository source dependency",
        )
    for dependency_id, authored in authored_sources.items():
        locked = locked_sources[dependency_id].dependency
        if (
            locked.repository_url != authored.repository_url
            or locked.revision_selector != authored.revision_selector
            or locked.dependency_kind != authored.dependency_kind
            or locked.optional != authored.optional
        ):
            fail(
                f"{path}.revision.repository_sources[{dependency_id}]",
                "must preserve the exact authored repository selector",
            )
        if authored.integration_contract is None:
            if locked.integration_contract is not None:
                fail(
                    f"{path}.revision.repository_sources[{dependency_id}]",
                    "cannot invent an integration contract",
                )
        else:
            if locked.integration_contract is None:
                fail(
                    f"{path}.revision.repository_sources[{dependency_id}]",
                    "must resolve the authored integration contract",
                )
            _require_selector_resolution(
                authored.integration_contract,
                locked.integration_contract,
                path=f"{path}.revision.repository_sources[{dependency_id}]",
            )

    authored_assets = {item.asset_id: item for item in authoring.assets}
    locked_assets = {item.selector.asset_id: item for item in revision.assets}
    if set(authored_assets) != set(locked_assets):
        fail(
            f"{path}.revision.assets",
            "must lock every and only authored asset selector",
        )
    for asset_id, authored in authored_assets.items():
        locked = locked_assets[asset_id]
        if locked.selector != authored:
            fail(
                f"{path}.revision.assets[{asset_id}]",
                "must preserve the exact authored asset selector",
            )
        if authored.pin is not None and authored.pin.uri != locked.blob.identity:
            fail(
                f"{path}.revision.assets[{asset_id}]",
                "locked asset bytes must match the authored pin",
            )

    interface_references = {item.uri: item for item in revision.public_interfaces}
    if len(interface_references) != len(revision.public_interfaces):
        fail(f"{path}.revision.public_interfaces", "interface URIs must be unique")
    bindings = {item.capability: item for item in node.interface_bindings}
    authored_interfaces = {
        item.name: item.interface
        for item in authoring.provides
        if item.interface is not None
    }
    if set(bindings) != set(authored_interfaces):
        fail(
            f"{path}.interface_bindings",
            "must bind every and only authored public capability interface",
        )
    used_references: set[str] = set()
    for capability, selector in authored_interfaces.items():
        assert selector is not None
        reference = interface_references.get(selector.uri)
        if reference is None:
            fail(
                f"{path}.revision.public_interfaces",
                "must resolve every authored public interface selector",
            )
        _require_selector_resolution(
            selector, reference, path=f"{path}.revision.public_interfaces"
        )
        if bindings[capability].interface_identity != reference.identity:
            fail(
                f"{path}.interface_bindings[{capability}]",
                "must bind the exact resolved public interface",
            )
        used_references.add(reference.uri)
    if used_references != set(interface_references):
        fail(
            f"{path}.revision.public_interfaces",
            "cannot contain an interface not selected by ComponentAuthoring",
        )


def _edge_key(edge: ExecutableComponentEdge) -> tuple[str, str, str, str]:
    return (
        edge.consumer_revision.uri,
        edge.provider_revision.uri,
        edge.requirement_id,
        edge.kind.value,
    )


def _require_closed_acyclic_graph(
    revisions: tuple[str, ...],
    edges: tuple[ExecutableComponentEdge, ...],
    *,
    root_revision: str,
) -> None:
    """Validate graph closure and cycles without recursion at the 4096-node bound."""

    adjacency: dict[str, set[str]] = {revision: set() for revision in revisions}
    indegree = {revision: 0 for revision in revisions}
    for edge in edges:
        consumer = edge.consumer_revision.uri
        provider = edge.provider_revision.uri
        if provider not in adjacency[consumer]:
            adjacency[consumer].add(provider)
            indegree[provider] += 1

    reachable: set[str] = set()
    pending = [root_revision]
    while pending:
        revision = pending.pop()
        if revision in reachable:
            continue
        reachable.add(revision)
        pending.extend(sorted(adjacency[revision], reverse=True))
    if reachable != set(revisions):
        fail(
            "ComponentLock.nodes",
            "every node must be reachable from the root revision",
        )

    ready = [revision for revision, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    visited = 0
    while ready:
        revision = heapq.heappop(ready)
        visited += 1
        for provider in sorted(adjacency[revision]):
            indegree[provider] -= 1
            if indegree[provider] == 0:
                heapq.heappush(ready, provider)
    if visited != len(revisions):
        fail("ComponentLock.edges", "must form an acyclic Component graph")


@dataclass(frozen=True, slots=True)
class ComponentLock:
    """Canonical selected derivation graph for one named target."""

    target_name: str
    target_profile_identity: ContentIdentity
    selection_policy_identity: ContentIdentity
    resolver_identity: ContentIdentity
    root_revision: ContentIdentity
    nodes: tuple[ComponentLockNode, ...]
    edges: tuple[ExecutableComponentEdge, ...]
    _authorings: tuple[ComponentAuthoring, ...] = field(repr=False, compare=False)
    provider_resolutions: tuple[ProviderResolution, ...] = ()

    SCHEMA: ClassVar[str] = COMPONENT_LOCK_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target_name, "ComponentLock.target_name")
        identity(self.target_profile_identity, "ComponentLock.target_profile_identity")
        identity(
            self.selection_policy_identity,
            "ComponentLock.selection_policy_identity",
        )
        identity(self.resolver_identity, "ComponentLock.resolver_identity")
        identity(self.root_revision, "ComponentLock.root_revision")
        nodes = _canonical_values(
            self.nodes,
            path="ComponentLock.nodes",
            expected_type=ComponentLockNode,
            key=lambda item: item.revision.identity.uri,
            label="locked Component revision identities",
            maximum=4096,
        )
        if not nodes:
            fail("ComponentLock.nodes", "must not be empty")
        by_revision = {item.revision.identity.uri: item for item in nodes}
        if self.root_revision.uri not in by_revision:
            fail(
                "ComponentLock.root_revision", "must identify a node in the exact graph"
            )
        if any(
            item.target_flavor_selection.target_name != self.target_name
            or item.target_flavor_selection.target_profile_identity
            != self.target_profile_identity
            or item.target_flavor_selection.selection_policy_identity
            != self.selection_policy_identity
            for item in nodes
        ):
            fail(
                "ComponentLock.nodes",
                "every node selection must use the lock's exact target and policy",
            )
        authorings = _canonical_values(
            self._authorings,
            path="ComponentLock.authorings",
            expected_type=ComponentAuthoring,
            key=lambda item: item.identity.uri,
            label="external ComponentAuthoring identities",
            maximum=4096,
        )
        by_authoring = {item.identity.uri: item for item in authorings}
        expected_authoring = {
            item.revision.authoring_identity.uri for item in self.nodes
        }
        if set(by_authoring) != expected_authoring or len(authorings) != len(nodes):
            fail(
                "ComponentLock.authorings",
                "must supply every and only exact external ComponentAuthoring value",
            )
        _canonical_values(
            self.provider_resolutions,
            path="ComponentLock.provider_resolutions",
            expected_type=ProviderResolution,
            key=lambda item: item.identity.uri,
            label="provider resolution identities",
            maximum=256,
        )
        for node in nodes:
            _require_node_authoring(
                node, by_authoring[node.revision.authoring_identity.uri]
            )
        edges = _canonical_values(
            self.edges,
            path="ComponentLock.edges",
            expected_type=ExecutableComponentEdge,
            key=_edge_key,
            label="executable edge keys",
            maximum=16384,
        )
        revisions = tuple(by_revision)
        requirement_keys = tuple(
            (edge.consumer_revision.uri, edge.requirement_id) for edge in edges
        )
        if len(set(requirement_keys)) != len(requirement_keys):
            fail(
                "ComponentLock.edges",
                "one consumer requirement cannot select multiple providers",
            )
        requirement_edges = set(requirement_keys)
        satisfactions = {
            (node.revision.identity.uri, item.requirement_id): item
            for node in nodes
            for item in node.requirement_constraint_satisfactions
        }
        for edge in edges:
            if (
                edge.consumer_revision.uri not in by_revision
                or edge.provider_revision.uri not in by_revision
            ):
                fail(
                    "ComponentLock.edges",
                    "cannot reference a revision outside the graph",
                )
            requirement_key = (edge.consumer_revision.uri, edge.requirement_id)
            consumer_node = by_revision[edge.consumer_revision.uri]
            consumer_authoring = by_authoring[
                consumer_node.revision.authoring_identity.uri
            ]
            requirement = next(
                (
                    item
                    for item in _effective_requirements(
                        consumer_node, consumer_authoring
                    )
                    if item.requirement_id == edge.requirement_id
                ),
                None,
            )
            if requirement is None:
                fail(
                    "ComponentLock.edges",
                    "cannot invent a requirement absent from Component or selected "
                    "Flavor authority",
                )
            if (
                edge.capability != requirement.capability
                or edge.kind != requirement.dependency_kind
                or edge.optional != requirement.optional
            ):
                fail(
                    "ComponentLock.edges",
                    "must preserve the authored requirement's exact edge semantics",
                )
            satisfaction = satisfactions.get(requirement_key)
            if satisfaction is None:
                fail(
                    "ComponentLock.edges",
                    "every selected requirement must have exact constraint "
                    "satisfaction evidence",
                )
            if satisfaction.provider_revision != edge.provider_revision:
                fail(
                    "ComponentLock.edges",
                    "constraint satisfaction must bind the selected provider",
                )
            if satisfaction.constraints != requirement.constraints:
                fail(
                    "ComponentLock.edges",
                    "constraint satisfaction must cover every and only authored "
                    "constraint in exact order",
                )
            provider_node = by_revision[edge.provider_revision.uri]
            provider_authoring = by_authoring[
                provider_node.revision.authoring_identity.uri
            ]
            provided = _provided_capabilities(provider_authoring).get(edge.capability)
            if provided is None:
                fail(
                    "ComponentLock.edges",
                    "selected provider does not declare the required capability",
                )
            try:
                version_matches = _version_satisfies(
                    provided.version, requirement.version_range
                )
            except ValueError as exc:
                fail(
                    "ComponentLock.edges",
                    f"authored requirement has an invalid version range: {exc}",
                )
            if not version_matches:
                fail(
                    "ComponentLock.edges",
                    "selected provider version does not satisfy the requirement",
                )
            if edge.public_interface_identity is not None:
                matches = tuple(
                    binding
                    for binding in provider_node.interface_bindings
                    if binding.capability == edge.capability
                    and binding.interface_identity == edge.public_interface_identity
                )
                if len(matches) != 1:
                    fail(
                        "ComponentLock.edges",
                        "generation edge must consume the provider's exact capability "
                        "interface binding",
                    )
        if set(satisfactions) != requirement_edges:
            fail(
                "ComponentLock.nodes",
                "constraint satisfactions must cover every and only selected "
                "requirement",
            )
        for node in nodes:
            node_authoring = by_authoring[node.revision.authoring_identity.uri]
            selected_requirements = {
                edge.requirement_id
                for edge in edges
                if edge.consumer_revision == node.revision.identity
            }
            required = {
                item.requirement_id
                for item in _effective_requirements(node, node_authoring)
                if not item.optional
            }
            if not required.issubset(selected_requirements):
                fail(
                    "ComponentLock.edges",
                    "every required Component or selected Flavor requirement must "
                    "select one provider",
                )
        _require_closed_acyclic_graph(
            revisions,
            edges,
            root_revision=self.root_revision.uri,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def selected_derivation_identity(self) -> ContentIdentity:
        """Name the lock identity without implying that catalog audit participates."""

        return self.identity

    @property
    def authorings(self) -> tuple[ComponentAuthoring, ...]:
        """Return required validation context that is excluded from lock identity."""

        return self._authorings

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target_name": self.target_name,
            "target_profile_identity": self.target_profile_identity.to_dict(),
            "selection_policy_identity": self.selection_policy_identity.to_dict(),
            "resolver_identity": self.resolver_identity.to_dict(),
            "root_revision": self.root_revision.to_dict(),
            "nodes": [item.to_dict() for item in self.nodes],
            "edges": [item.to_dict() for item in self.edges],
            "provider_resolutions": [
                item.to_dict() for item in self.provider_resolutions
            ],
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        authorings: tuple[ComponentAuthoring, ...],
        path: str = "ComponentLock",
    ) -> ComponentLock:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "target_name",
                    "target_profile_identity",
                    "selection_policy_identity",
                    "resolver_identity",
                    "root_revision",
                    "nodes",
                    "edges",
                }
            ),
            optional=frozenset({"provider_resolutions"}),
        )
        return cls(
            target_name=portable_name(data["target_name"], f"{path}.target_name"),
            target_profile_identity=ContentIdentity.from_dict(
                data["target_profile_identity"],
                path=f"{path}.target_profile_identity",
            ),
            selection_policy_identity=ContentIdentity.from_dict(
                data["selection_policy_identity"],
                path=f"{path}.selection_policy_identity",
            ),
            resolver_identity=ContentIdentity.from_dict(
                data["resolver_identity"], path=f"{path}.resolver_identity"
            ),
            root_revision=ContentIdentity.from_dict(
                data["root_revision"], path=f"{path}.root_revision"
            ),
            nodes=parse_tuple(
                data["nodes"],
                f"{path}.nodes",
                lambda value, *, path: ComponentLockNode.from_dict(
                    value,
                    definitions=authorings,
                    path=path,
                ),
            ),
            edges=parse_tuple(
                data["edges"], f"{path}.edges", ExecutableComponentEdge.from_dict
            ),
            _authorings=authorings,
            provider_resolutions=parse_tuple(
                data.get("provider_resolutions", ()),
                f"{path}.provider_resolutions",
                ProviderResolution.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class NodeFlavorCandidateAudit:
    """One selected or rejected discovery decision, deliberately outside the lock."""

    component_revision: ContentIdentity
    slot_id: str
    flavor_revision: ContentIdentity
    status: CandidateStatus
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identity(self.component_revision, "NodeFlavorCandidateAudit.component_revision")
        portable_name(self.slot_id, "NodeFlavorCandidateAudit.slot_id")
        identity(self.flavor_revision, "NodeFlavorCandidateAudit.flavor_revision")
        if not isinstance(self.status, CandidateStatus):
            fail("NodeFlavorCandidateAudit.status", "must be a CandidateStatus")
        reasons = texts(
            self.reasons, "NodeFlavorCandidateAudit.reasons", maximum_items=64
        )
        if reasons != tuple(sorted(reasons)):
            fail("NodeFlavorCandidateAudit.reasons", "must use canonical reason order")
        if self.status is CandidateStatus.SELECTED and reasons:
            fail(
                "NodeFlavorCandidateAudit.reasons",
                "selected candidates cannot carry rejection reasons",
            )
        if self.status is not CandidateStatus.SELECTED and not reasons:
            fail(
                "NodeFlavorCandidateAudit.reasons",
                "unselected candidates require an explanation",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "component_revision": self.component_revision.to_dict(),
            "slot_id": self.slot_id,
            "flavor_revision": self.flavor_revision.to_dict(),
            "status": self.status.value,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "NodeFlavorCandidateAudit"
    ) -> NodeFlavorCandidateAudit:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "component_revision",
                    "slot_id",
                    "flavor_revision",
                    "status",
                    "reasons",
                }
            ),
        )
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            slot_id=portable_name(data["slot_id"], f"{path}.slot_id"),
            flavor_revision=ContentIdentity.from_dict(
                data["flavor_revision"], path=f"{path}.flavor_revision"
            ),
            status=enum_value(CandidateStatus, data["status"], f"{path}.status"),
            reasons=string_tuple(data["reasons"], f"{path}.reasons"),
        )


@dataclass(frozen=True, slots=True)
class ComponentResolutionAudit:
    """Explain discovery without making rejected candidates derivation authority."""

    component_lock_identity: ContentIdentity
    catalog_identity: ContentIdentity
    resolver_identity: ContentIdentity
    candidates: tuple[NodeFlavorCandidateAudit, ...]

    SCHEMA: ClassVar[str] = COMPONENT_RESOLUTION_AUDIT_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.component_lock_identity,
            "ComponentResolutionAudit.component_lock_identity",
        )
        identity(self.catalog_identity, "ComponentResolutionAudit.catalog_identity")
        identity(self.resolver_identity, "ComponentResolutionAudit.resolver_identity")
        _canonical_values(
            self.candidates,
            path="ComponentResolutionAudit.candidates",
            expected_type=NodeFlavorCandidateAudit,
            key=lambda item: (
                item.component_revision.uri,
                item.slot_id,
                item.flavor_revision.uri,
            ),
            label="candidate decisions",
            maximum=16384,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "catalog_identity": self.catalog_identity.to_dict(),
            "resolver_identity": self.resolver_identity.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentResolutionAudit"
    ) -> ComponentResolutionAudit:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_lock_identity",
                    "catalog_identity",
                    "resolver_identity",
                    "candidates",
                }
            ),
        )
        return cls(
            component_lock_identity=ContentIdentity.from_dict(
                data["component_lock_identity"],
                path=f"{path}.component_lock_identity",
            ),
            catalog_identity=ContentIdentity.from_dict(
                data["catalog_identity"], path=f"{path}.catalog_identity"
            ),
            resolver_identity=ContentIdentity.from_dict(
                data["resolver_identity"], path=f"{path}.resolver_identity"
            ),
            candidates=parse_tuple(
                data["candidates"],
                f"{path}.candidates",
                NodeFlavorCandidateAudit.from_dict,
            ),
        )


__all__ = [
    "COMPONENT_LOCK_NODE_SCHEMA",
    "COMPONENT_LOCK_SCHEMA",
    "COMPONENT_RESOLUTION_AUDIT_SCHEMA",
    "LOCKED_COMPONENT_REVISION_SCHEMA",
    "RESOLVED_COMPONENT_ASSET_SCHEMA",
    "ComponentLock",
    "ComponentLockNode",
    "ComponentResolutionAudit",
    "LockedComponentRevision",
    "NodeFlavorCandidateAudit",
    "RequirementConstraintSatisfaction",
    "ResolvedComponentAsset",
    "ordered_specification_set_identity",
]
