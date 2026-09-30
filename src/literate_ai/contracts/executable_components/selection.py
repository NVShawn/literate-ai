"""Per-Component target and cardinality-aware Flavor-slot resolution contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from .._validation import (
    contract_fields,
    fail,
    fields,
    parse_tuple,
    string_value,
    unique,
)
from ..flavors import FlavorCardinality, FlavorSlot
from ..identity import ContentIdentity, contract_identity
from ._common import identity, portable_name, tuple_value

NODE_TARGET_FLAVOR_SELECTION_SCHEMA = (
    "urn:literate-ai:schema:v2:node-target-flavor-selection"
)


@dataclass(frozen=True, slots=True)
class SelectedNodeFlavor:
    """One exact Flavor selected for one declared Component slot."""

    value: str
    flavor_revision: ContentIdentity

    def __post_init__(self) -> None:
        string_value(self.value, "SelectedNodeFlavor.value")
        identity(self.flavor_revision, "SelectedNodeFlavor.flavor_revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "flavor_revision": self.flavor_revision.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SelectedNodeFlavor"
    ) -> SelectedNodeFlavor:
        data = fields(
            value,
            path=path,
            required=frozenset({"value", "flavor_revision"}),
        )
        return cls(
            string_value(data["value"], f"{path}.value"),
            ContentIdentity.from_dict(
                data["flavor_revision"], path=f"{path}.flavor_revision"
            ),
        )


@dataclass(frozen=True, slots=True)
class NodeFlavorSlotResolution:
    """One declared slot and its complete zero, one, or many selection result."""

    slot: FlavorSlot
    selected: tuple[SelectedNodeFlavor, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.slot, FlavorSlot):
            fail("NodeFlavorSlotResolution.slot", "must be a FlavorSlot")
        values = tuple_value(self.selected, "NodeFlavorSlotResolution.selected")
        if len(values) > 64 or any(
            not isinstance(item, SelectedNodeFlavor) for item in values
        ):
            fail(
                "NodeFlavorSlotResolution.selected",
                "must contain at most 64 SelectedNodeFlavor values",
            )
        keys = tuple((item.value, item.flavor_revision.uri) for item in self.selected)
        unique(
            tuple(item.value for item in self.selected),
            "NodeFlavorSlotResolution.selected",
            "Flavor values",
        )
        unique(
            tuple(item.flavor_revision.uri for item in self.selected),
            "NodeFlavorSlotResolution.selected",
            "Flavor revisions",
        )
        if keys != tuple(sorted(keys)):
            fail(
                "NodeFlavorSlotResolution.selected",
                "must use canonical Flavor selection order",
            )
        count = len(self.selected)
        accepted = {
            FlavorCardinality.EXACTLY_ONE: count == 1,
            FlavorCardinality.ZERO_OR_ONE: count <= 1,
            FlavorCardinality.ONE_OR_MORE: count >= 1,
            FlavorCardinality.BOUNDED: (
                self.slot.minimum is not None
                and self.slot.maximum is not None
                and self.slot.minimum <= count <= self.slot.maximum
            ),
        }[self.slot.cardinality]
        if not accepted:
            expected = (
                f"{self.slot.minimum}..{self.slot.maximum}"
                if self.slot.cardinality is FlavorCardinality.BOUNDED
                else self.slot.cardinality.value
            )
            fail(
                "NodeFlavorSlotResolution.selected",
                f"slot {self.slot.slot_id!r} requires {expected}; selected {count}",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot.to_dict(),
            "selected": [item.to_dict() for item in self.selected],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "NodeFlavorSlotResolution"
    ) -> NodeFlavorSlotResolution:
        data = fields(
            value,
            path=path,
            required=frozenset({"slot", "selected"}),
        )
        return cls(
            slot=FlavorSlot.from_dict(data["slot"], path=f"{path}.slot"),
            selected=parse_tuple(
                data["selected"],
                f"{path}.selected",
                SelectedNodeFlavor.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class NodeTargetFlavorSelection:
    """The target and complete declared-slot result for one graph node."""

    component_revision: ContentIdentity
    target_name: str
    target_profile_identity: ContentIdentity
    selection_policy_identity: ContentIdentity
    slots: tuple[NodeFlavorSlotResolution, ...]

    SCHEMA: ClassVar[str] = NODE_TARGET_FLAVOR_SELECTION_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.component_revision,
            "NodeTargetFlavorSelection.component_revision",
        )
        portable_name(self.target_name, "NodeTargetFlavorSelection.target_name")
        identity(
            self.target_profile_identity,
            "NodeTargetFlavorSelection.target_profile_identity",
        )
        identity(
            self.selection_policy_identity,
            "NodeTargetFlavorSelection.selection_policy_identity",
        )
        slots = tuple_value(self.slots, "NodeTargetFlavorSelection.slots")
        if len(slots) > 64 or any(
            not isinstance(item, NodeFlavorSlotResolution) for item in slots
        ):
            fail(
                "NodeTargetFlavorSelection.slots",
                "must contain at most 64 NodeFlavorSlotResolution values",
            )
        slot_ids = tuple(item.slot.slot_id for item in self.slots)
        unique(slot_ids, "NodeTargetFlavorSelection.slots", "declared slot IDs")
        if slot_ids != tuple(sorted(slot_ids)):
            fail(
                "NodeTargetFlavorSelection.slots",
                "must represent every declared slot once in canonical order",
            )

    def require_slots(self, declared: tuple[FlavorSlot, ...]) -> None:
        """Fail when this result does not represent the exact Component declaration."""

        expected = {item.slot_id: item for item in declared}
        actual = {item.slot.slot_id: item.slot for item in self.slots}
        if actual != expected:
            fail(
                "NodeTargetFlavorSelection.slots",
                "must represent every and only declared Component Flavor slot",
            )

    @property
    def selected_flavor_revisions(self) -> tuple[ContentIdentity, ...]:
        by_uri = {
            item.flavor_revision.uri: item.flavor_revision
            for slot in self.slots
            for item in slot.selected
        }
        return tuple(sorted(by_uri.values(), key=lambda item: item.uri))

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "target_name": self.target_name,
            "target_profile_identity": self.target_profile_identity.to_dict(),
            "selection_policy_identity": self.selection_policy_identity.to_dict(),
            "slots": [item.to_dict() for item in self.slots],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "NodeTargetFlavorSelection"
    ) -> NodeTargetFlavorSelection:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "target_name",
                    "target_profile_identity",
                    "selection_policy_identity",
                    "slots",
                }
            ),
        )
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
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
            slots=parse_tuple(
                data["slots"],
                f"{path}.slots",
                NodeFlavorSlotResolution.from_dict,
            ),
        )


__all__ = [
    "NODE_TARGET_FLAVOR_SELECTION_SCHEMA",
    "NodeFlavorSlotResolution",
    "NodeTargetFlavorSelection",
    "SelectedNodeFlavor",
]
