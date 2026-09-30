"""Neutral catalog descriptors and machine-local materialization metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from literate_ai.contracts import (
    ComponentDefinition,
    ComponentRevisionRef,
    ContentIdentity,
    FlavorDefinition,
    canonical_identity,
)


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    POLICY_DENIED = "policy-denied"


@dataclass(frozen=True, slots=True)
class AvailabilityReason:
    code: str
    message: str

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("availability reason code and message must not be empty")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class DescriptorAttribute:
    key: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.key or not self.values or any(not value for value in self.values):
            raise ValueError("descriptor attributes require a key and non-empty values")
        if len(set(self.values)) != len(self.values):
            raise ValueError("descriptor attribute values must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "values": list(self.values)}


@dataclass(frozen=True, slots=True)
class ComponentDescriptor:
    revision_identity: ContentIdentity
    definition: ComponentDefinition
    availability: AvailabilityStatus = AvailabilityStatus.AVAILABLE
    availability_reasons: tuple[AvailabilityReason, ...] = ()
    attributes: tuple[DescriptorAttribute, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.availability is AvailabilityStatus.AVAILABLE
            and self.availability_reasons
        ):
            raise ValueError("available descriptors cannot have unavailability reasons")
        if (
            self.availability is not AvailabilityStatus.AVAILABLE
            and not self.availability_reasons
        ):
            raise ValueError("unavailable descriptors require at least one reason")
        if len({item.key for item in self.attributes}) != len(self.attributes):
            raise ValueError("descriptor attribute keys must be unique")

    @property
    def coordinate(self) -> str:
        return self.definition.coordinate.uri

    @property
    def ref(self) -> ComponentRevisionRef:
        return ComponentRevisionRef(
            self.definition.coordinate,
            self.definition.version,
            self.revision_identity,
        )

    @property
    def is_available(self) -> bool:
        return self.availability is AvailabilityStatus.AVAILABLE

    def attribute_values(self, key: str) -> tuple[str, ...] | None:
        return next((item.values for item in self.attributes if item.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "component",
            "revision_identity": self.revision_identity.to_dict(),
            "definition_identity": self.definition.identity.to_dict(),
            "coordinate": self.coordinate,
            "version": self.definition.version,
            "provides": [item.to_dict() for item in self.definition.provides],
            "requires": [item.to_dict() for item in self.definition.requires],
            "availability": self.availability.value,
            "availability_reasons": [
                item.to_dict() for item in self.availability_reasons
            ],
            "attributes": [item.to_dict() for item in self.attributes],
        }


@dataclass(frozen=True, slots=True)
class FlavorDescriptor:
    revision_identity: ContentIdentity
    definition: FlavorDefinition
    axis_value: str
    availability: AvailabilityStatus = AvailabilityStatus.AVAILABLE
    availability_reasons: tuple[AvailabilityReason, ...] = ()
    attributes: tuple[DescriptorAttribute, ...] = ()

    def __post_init__(self) -> None:
        if not self.axis_value:
            raise ValueError("Flavor descriptor axis_value must not be empty")
        if (
            self.availability is AvailabilityStatus.AVAILABLE
            and self.availability_reasons
        ):
            raise ValueError("available descriptors cannot have unavailability reasons")
        if (
            self.availability is not AvailabilityStatus.AVAILABLE
            and not self.availability_reasons
        ):
            raise ValueError("unavailable descriptors require at least one reason")

    @property
    def coordinate(self) -> str:
        return self.definition.coordinate.uri

    @property
    def is_available(self) -> bool:
        return self.availability is AvailabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "flavor",
            "revision_identity": self.revision_identity.to_dict(),
            "definition_identity": self.definition.identity.to_dict(),
            "coordinate": self.coordinate,
            "version": self.definition.version,
            "primary_axis": self.definition.primary_axis.value,
            "axis_value": self.axis_value,
            "availability": self.availability.value,
            "availability_reasons": [
                item.to_dict() for item in self.availability_reasons
            ],
            "attributes": [item.to_dict() for item in self.attributes],
        }


@dataclass(frozen=True, slots=True)
class DescriptorVocabulary:
    components: tuple[ComponentDescriptor, ...]
    flavors: tuple[FlavorDescriptor, ...]

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": [item.to_dict() for item in self.components],
            "flavors": [item.to_dict() for item in self.flavors],
        }


class MaterializationState(StrEnum):
    ABSENT = "absent"
    PARTIAL = "partial"
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class MaterializationMetadata:
    """A non-canonical projection; it never changes descriptor or revision identity."""

    revision_identity: ContentIdentity
    state: MaterializationState
    required_objects: tuple[ContentIdentity, ...]
    present_objects: tuple[ContentIdentity, ...]
    reasons: tuple[AvailabilityReason, ...] = ()

    @property
    def missing_objects(self) -> tuple[ContentIdentity, ...]:
        present = {item.uri for item in self.present_objects}
        return tuple(item for item in self.required_objects if item.uri not in present)


__all__ = [
    "AvailabilityReason",
    "AvailabilityStatus",
    "ComponentDescriptor",
    "DescriptorAttribute",
    "DescriptorVocabulary",
    "FlavorDescriptor",
    "MaterializationMetadata",
    "MaterializationState",
]
