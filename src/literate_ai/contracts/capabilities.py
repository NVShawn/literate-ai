"""Versioned capability, requirement, and resolved dependency-edge contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from .identity import SCHEMA_PREFIX, ContentIdentity, contract_identity

CAPABILITY_SCHEMA = f"{SCHEMA_PREFIX}capability"
CAPABILITY_REQUIREMENT_SCHEMA = f"{SCHEMA_PREFIX}capability-requirement"
DEPENDENCY_EDGE_SCHEMA = f"{SCHEMA_PREFIX}dependency-edge"


class DependencyKind(StrEnum):
    GENERATION = "generation"
    BUILD = "build"
    RUNTIME = "runtime"
    VALIDATION = "validation"
    TOOLCHAIN = "toolchain"
    PACKAGING = "packaging"
    DEPLOYMENT = "deployment"


@dataclass(frozen=True, slots=True)
class CapabilityConstraint:
    """A typed, declarative constraint; adapters interpret registered operators."""

    key: str
    operator: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        string_value(self.key, "CapabilityConstraint.key")
        string_value(self.operator, "CapabilityConstraint.operator")
        if not self.values:
            fail("CapabilityConstraint.values", "must not be empty")
        unique(self.values, "CapabilityConstraint.values")
        for index, value in enumerate(self.values):
            string_value(value, f"CapabilityConstraint.values[{index}]")

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "operator": self.operator, "values": list(self.values)}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CapabilityConstraint"
    ) -> CapabilityConstraint:
        data = fields(
            value,
            path=path,
            required=frozenset({"key", "operator", "values"}),
        )
        return cls(
            key=string_value(data["key"], f"{path}.key"),
            operator=string_value(data["operator"], f"{path}.operator"),
            values=string_tuple(data["values"], f"{path}.values"),
        )


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    version: str
    contract: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = CAPABILITY_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.name, "Capability.name")
        string_value(self.version, "Capability.version")
        if not isinstance(self.contract, (ContentIdentity, type(None))):
            fail("Capability.contract", "must be a ContentIdentity or null")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "name": self.name,
            "version": self.version,
            "contract": None if self.contract is None else self.contract.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "Capability") -> Capability:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"name", "version", "contract"}),
        )
        contract = data["contract"]
        return cls(
            name=string_value(data["name"], f"{path}.name"),
            version=string_value(data["version"], f"{path}.version"),
            contract=(
                None
                if contract is None
                else ContentIdentity.from_dict(contract, path=f"{path}.contract")
            ),
        )


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    requirement_id: str
    capability: str
    version_range: str
    dependency_kind: DependencyKind
    optional: bool = False
    constraints: tuple[CapabilityConstraint, ...] = ()

    SCHEMA: ClassVar[str] = CAPABILITY_REQUIREMENT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.requirement_id, "CapabilityRequirement.requirement_id")
        string_value(self.capability, "CapabilityRequirement.capability")
        string_value(self.version_range, "CapabilityRequirement.version_range")
        if not isinstance(self.dependency_kind, DependencyKind):
            fail("CapabilityRequirement.dependency_kind", "must be a DependencyKind")
        if not isinstance(self.optional, bool):
            fail("CapabilityRequirement.optional", "must be a boolean")
        unique(
            tuple(constraint.key for constraint in self.constraints),
            "CapabilityRequirement.constraints",
            "constraint keys",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "requirement_id": self.requirement_id,
            "capability": self.capability,
            "version_range": self.version_range,
            "dependency_kind": self.dependency_kind.value,
            "optional": self.optional,
            "constraints": [constraint.to_dict() for constraint in self.constraints],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CapabilityRequirement"
    ) -> CapabilityRequirement:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "requirement_id",
                    "capability",
                    "version_range",
                    "dependency_kind",
                    "optional",
                    "constraints",
                }
            ),
        )
        optional = data["optional"]
        if not isinstance(optional, bool):
            fail(f"{path}.optional", "must be a boolean")
        return cls(
            requirement_id=string_value(
                data["requirement_id"], f"{path}.requirement_id"
            ),
            capability=string_value(data["capability"], f"{path}.capability"),
            version_range=string_value(data["version_range"], f"{path}.version_range"),
            dependency_kind=enum_value(
                DependencyKind, data["dependency_kind"], f"{path}.dependency_kind"
            ),
            optional=optional,
            constraints=parse_tuple(
                data["constraints"],
                f"{path}.constraints",
                CapabilityConstraint.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """An exact resolved forward edge between two immutable revisions."""

    source_revision: ContentIdentity
    target_revision: ContentIdentity
    requirement: CapabilityRequirement
    provided_capability: Capability
    resolution_decision: ContentIdentity

    SCHEMA: ClassVar[str] = DEPENDENCY_EDGE_SCHEMA

    def __post_init__(self) -> None:
        if self.requirement.capability != self.provided_capability.name:
            fail(
                "DependencyEdge.provided_capability",
                "name must satisfy the requirement capability name",
            )

    @property
    def kind(self) -> DependencyKind:
        return self.requirement.dependency_kind

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "source_revision": self.source_revision.to_dict(),
            "target_revision": self.target_revision.to_dict(),
            "requirement": self.requirement.to_dict(),
            "provided_capability": self.provided_capability.to_dict(),
            "resolution_decision": self.resolution_decision.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "DependencyEdge") -> DependencyEdge:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "source_revision",
                    "target_revision",
                    "requirement",
                    "provided_capability",
                    "resolution_decision",
                }
            ),
        )
        return cls(
            source_revision=ContentIdentity.from_dict(
                data["source_revision"], path=f"{path}.source_revision"
            ),
            target_revision=ContentIdentity.from_dict(
                data["target_revision"], path=f"{path}.target_revision"
            ),
            requirement=CapabilityRequirement.from_dict(
                data["requirement"], path=f"{path}.requirement"
            ),
            provided_capability=Capability.from_dict(
                data["provided_capability"], path=f"{path}.provided_capability"
            ),
            resolution_decision=ContentIdentity.from_dict(
                data["resolution_decision"], path=f"{path}.resolution_decision"
            ),
        )


__all__ = [
    "CAPABILITY_REQUIREMENT_SCHEMA",
    "CAPABILITY_SCHEMA",
    "DEPENDENCY_EDGE_SCHEMA",
    "Capability",
    "CapabilityConstraint",
    "CapabilityRequirement",
    "DependencyEdge",
    "DependencyKind",
]
