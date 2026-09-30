"""Provider-neutral ordered Flavor selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from literate_ai.contracts.flavors import FlavorSelectionCandidate
from literate_ai.contracts.identity import ComponentCoordinate

_AXIS_SELECTOR_PREFIXES = {
    "implementation.language-ecosystem": "lang",
    "implementation.ui-framework": "ui",
    "platform.os": "os",
    "build.system": "build",
    "toolchain": "toolchain",
    "packaging": "package",
}


def flavor_candidate_aliases(flavor: FlavorSelectionCandidate) -> frozenset[str]:
    """Return short, qualified, and canonical selectors for one Flavor."""

    aliases = {
        flavor.flavor_id,
        flavor.value,
        f"{flavor.axis}={flavor.value}",
    }
    prefix = _AXIS_SELECTOR_PREFIXES.get(flavor.axis)
    if prefix is not None:
        aliases.add(f"{prefix}-{flavor.value}")
    if flavor.coordinate_uri is not None:
        aliases.add(flavor.coordinate_uri)
    return frozenset(aliases)


def parse_flavor_selector_body(
    selection: str, *, known_slots: frozenset[str] | None = None
) -> tuple[str | None, str]:
    """Split an optional Component slot without misreading ``flavor://`` URIs."""

    if not selection:
        raise FlavorSelectionError(
            "flavor_selection.invalid_flavor_selector",
            "Flavor selector must name a Flavor",
        )
    if selection.startswith("flavor://"):
        return None, selection
    slot_id, separator, alias = selection.partition(":")
    if not separator:
        return None, selection
    if not slot_id or not alias:
        raise FlavorSelectionError(
            "flavor_selection.invalid_flavor_selector",
            "slot-qualified Flavor selectors require both slot and Flavor",
        )
    if ":" in alias and not alias.startswith("flavor://"):
        raise FlavorSelectionError(
            "flavor_selection.invalid_flavor_selector",
            "slot-qualified Flavor selector has an invalid Flavor name",
        )
    if known_slots is not None and slot_id not in known_slots:
        raise FlavorSelectionError(
            "flavor_selection.unknown_flavor_slot",
            f"Component Flavor slot is unknown: {slot_id}",
        )
    return slot_id, alias


def parse_scoped_flavor_selector(selector: str) -> tuple[str | None, str]:
    """Split an optional Component coordinate from one effective selector.

    Coordinate scope is orthogonal to the existing operation, slot, and Flavor
    grammar. Keeping this split in the selection application layer ensures lock
    planning and later authority replay interpret the exact request identically.
    """

    coordinate_text, separator, effective = selector.rpartition("::")
    if not separator:
        return None, selector
    if not coordinate_text or not effective:
        raise FlavorSelectionError(
            "flavor_selection.flavor_selector_invalid",
            "Coordinate-qualified Flavor selector is incomplete",
        )
    try:
        coordinate = ComponentCoordinate.parse(coordinate_text)
    except ValueError as exc:
        raise FlavorSelectionError(
            "flavor_selection.flavor_selector_component_invalid",
            "Coordinate-qualified Flavor selector has an invalid Component coordinate",
        ) from exc
    return coordinate.uri, effective


class FlavorSelectionError(RuntimeError):
    """An ordered Flavor selection request is invalid or contradictory."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def apply_flavor_selectors(
    catalog: Sequence[FlavorSelectionCandidate],
    selectors: Sequence[str],
    *,
    initial: Sequence[FlavorSelectionCandidate] = (),
    initial_is_preferences: bool = False,
    multi_value_axes: frozenset[str] = frozenset(),
    slot_axes: Mapping[str, str] | None = None,
    multi_value_slots: frozenset[str] = frozenset(),
) -> tuple[FlavorSelectionCandidate, ...]:
    """Apply ordered selectors and retain exact Component-slot bindings.

    Unqualified ``+flavor`` and ``-flavor`` selectors retain their original behavior
    when an axis has one Component slot. An axis used by multiple slots requires
    ``+slot-id:flavor`` so role assignment cannot be inferred from selector order.
    Initial preferences are weaker than explicit selections and may be replaced
    without first being subtracted.
    """

    aliases: dict[str, FlavorSelectionCandidate] = {}
    ambiguous: set[str] = set()
    for flavor in catalog:
        for alias in flavor_candidate_aliases(flavor):
            if alias in aliases and aliases[alias] != flavor:
                ambiguous.add(alias)
            aliases[alias] = flavor
    configured_slot_axes = {} if slot_axes is None else dict(slot_axes)
    slots_by_axis: dict[str, tuple[str, ...]] = {}
    for slot_id, axis in configured_slot_axes.items():
        slots_by_axis[axis] = (*slots_by_axis.get(axis, ()), slot_id)

    def require_valid_binding(flavor: FlavorSelectionCandidate) -> None:
        if not flavor.slot_ids:
            if len(slots_by_axis.get(flavor.axis, ())) > 1:
                raise FlavorSelectionError(
                    "flavor_selection.flavor_slot_required",
                    f"axis {flavor.axis} has multiple Component slots; select "
                    "each Flavor as +slot-id:flavor",
                )
            return
        for slot_id in flavor.slot_ids:
            expected_axis = configured_slot_axes.get(slot_id)
            if expected_axis is None:
                raise FlavorSelectionError(
                    "flavor_selection.unknown_flavor_slot",
                    f"Component Flavor slot is unknown: {slot_id}",
                )
            if expected_axis != flavor.axis:
                raise FlavorSelectionError(
                    "flavor_selection.flavor_slot_axis_mismatch",
                    f"Flavor {flavor.flavor_id} targets {flavor.axis}, not slot "
                    f"{slot_id} on {expected_axis}",
                )

    def same_selection_location(
        left: FlavorSelectionCandidate, right: FlavorSelectionCandidate
    ) -> bool:
        if left.axis != right.axis:
            return False
        if left.slot_ids and right.slot_ids:
            return any(
                slot_id not in multi_value_slots
                for slot_id in set(left.slot_ids).intersection(right.slot_ids)
            )
        return left.axis not in multi_value_axes

    def selection_locations(
        flavor: FlavorSelectionCandidate,
    ) -> frozenset[tuple[str, str | None]]:
        if flavor.slot_ids:
            return frozenset((flavor.axis, slot_id) for slot_id in flavor.slot_ids)
        return frozenset({(flavor.axis, None)})

    if not isinstance(initial_is_preferences, bool):
        raise TypeError("initial_is_preferences must be a boolean")
    preference_axes = (
        frozenset(flavor.axis for flavor in initial)
        if initial_is_preferences
        else frozenset()
    )
    normalized_initial = tuple(
        replace(flavor, slot_ids=slots_by_axis[flavor.axis])
        if (
            flavor.axis in preference_axes
            and not flavor.slot_ids
            and len(slots_by_axis.get(flavor.axis, ())) == 1
        )
        else flavor
        for flavor in initial
    )
    initial_ids = tuple(flavor.flavor_id for flavor in normalized_initial)
    preference_locations = (
        {
            location
            for flavor in normalized_initial
            for location in selection_locations(flavor)
        }
        if initial_is_preferences
        else set()
    )
    if len(set(initial_ids)) != len(initial_ids):
        raise FlavorSelectionError(
            "flavor_selection.duplicate_flavor",
            "initial Flavors contain duplicate identities",
        )
    for flavor in normalized_initial:
        require_valid_binding(flavor)
    # Mutual exclusion among the initial set is validated once, after every
    # selector (including CLI overrides) has been applied, not here. A
    # singleton-axis default set legitimately names more than one candidate
    # when initial_is_preferences narrows the choice via a later +/-flavor
    # override; rejecting unconditionally at this point fired before any
    # override was even read, so no override could ever disambiguate it
    # (see issue #46). The shape check below is independent of that
    # resolution and still applies immediately.
    for index, flavor in enumerate(normalized_initial):
        for other in normalized_initial[index + 1 :]:
            if flavor.axis == other.axis and (
                bool(flavor.slot_ids) != bool(other.slot_ids)
            ):
                raise FlavorSelectionError(
                    "flavor_selection.flavor_slot_binding_ambiguous",
                    "initial Flavors mix qualified and unqualified selections on "
                    "one axis",
                )
    selected = {flavor.flavor_id: flavor for flavor in normalized_initial}
    for selector in selectors:
        if len(selector) < 2 or selector[0] not in {"+", "-"}:
            raise FlavorSelectionError(
                "flavor_selection.invalid_flavor_selector",
                "Flavor selectors must use +flavor, -flavor, "
                "+slot-id:flavor, or -slot-id:flavor",
            )
        slot_id, alias = parse_flavor_selector_body(
            selector[1:], known_slots=frozenset(configured_slot_axes)
        )
        if alias in ambiguous:
            coordinates = sorted(
                flavor.coordinate_uri or flavor.flavor_id
                for flavor in catalog
                if alias in flavor_candidate_aliases(flavor)
            )
            raise FlavorSelectionError(
                "flavor_selection.unknown_flavor",
                f"Flavor selector {alias!r} is ambiguous; use one of: "
                + ", ".join(coordinates),
            )
        if alias not in aliases:
            raise FlavorSelectionError(
                "flavor_selection.unknown_flavor",
                f"Flavor selector is unknown: {alias}",
            )
        flavor = aliases[alias]
        if selector[0] == "-" and slot_id is None:
            existing = selected.get(flavor.flavor_id)
            if existing is not None:
                preference_locations.difference_update(selection_locations(existing))
            selected.pop(flavor.flavor_id, None)
            continue
        if slot_id is None and len(slots_by_axis.get(flavor.axis, ())) > 1:
            raise FlavorSelectionError(
                "flavor_selection.flavor_slot_required",
                f"axis {flavor.axis} has multiple Component slots; select "
                "each Flavor as +slot-id:flavor",
            )
        singleton_slots = slots_by_axis.get(flavor.axis, ())
        effective_slot_id = (
            singleton_slots[0]
            if (
                slot_id is None
                and flavor.axis in preference_axes
                and len(singleton_slots) == 1
            )
            else slot_id
        )
        bound = replace(
            flavor,
            slot_ids=(() if effective_slot_id is None else (effective_slot_id,)),
        )
        require_valid_binding(bound)
        if selector[0] == "-":
            existing = selected.get(flavor.flavor_id)
            if existing is None:
                continue
            assert slot_id is not None
            if slot_id not in existing.slot_ids:
                raise FlavorSelectionError(
                    "flavor_selection.flavor_slot_binding_mismatch",
                    f"Flavor {flavor.flavor_id} is not selected for slot {slot_id}",
                )
            remaining_slots = tuple(
                item for item in existing.slot_ids if item != slot_id
            )
            if remaining_slots:
                selected[flavor.flavor_id] = replace(existing, slot_ids=remaining_slots)
            else:
                selected.pop(flavor.flavor_id)
            preference_locations.discard((flavor.axis, slot_id))
            continue
        selected_flavor = selected.get(flavor.flavor_id)
        if selected_flavor is not None and (
            bool(selected_flavor.slot_ids) != bool(bound.slot_ids)
        ):
            raise FlavorSelectionError(
                "flavor_selection.flavor_slot_binding_ambiguous",
                f"Flavor {flavor.flavor_id} cannot mix qualified and "
                "unqualified selection",
            )
        if selected_flavor is not None and slot_id in selected_flavor.slot_ids:
            preference_locations.difference_update(selection_locations(bound))
            continue
        if selected_flavor is not None and slot_id is None:
            preference_locations.difference_update(selection_locations(bound))
            continue
        if any(
            item.axis == bound.axis and bool(item.slot_ids) != bool(bound.slot_ids)
            for item in selected.values()
        ):
            raise FlavorSelectionError(
                "flavor_selection.flavor_slot_binding_ambiguous",
                f"axis {bound.axis} cannot mix qualified and unqualified "
                "Flavor selections",
            )
        existing = next(
            (
                item
                for item in selected.values()
                if item.flavor_id != bound.flavor_id
                and same_selection_location(item, bound)
            ),
            None,
        )
        if existing is not None:
            overlap = selection_locations(existing).intersection(
                selection_locations(bound)
            )
            if overlap and overlap.issubset(preference_locations):
                if existing.slot_ids:
                    remaining_slots = tuple(
                        item
                        for item in existing.slot_ids
                        if (existing.axis, item) not in overlap
                    )
                    if remaining_slots:
                        selected[existing.flavor_id] = replace(
                            existing, slot_ids=remaining_slots
                        )
                    else:
                        selected.pop(existing.flavor_id)
                else:
                    selected.pop(existing.flavor_id)
                preference_locations.difference_update(overlap)
                existing = None
        if existing is not None:
            location = (
                f"slot {effective_slot_id}"
                if effective_slot_id is not None
                else f"axis {flavor.axis}"
            )
            raise FlavorSelectionError(
                "flavor_selection.mutually_exclusive_flavors",
                f"{existing.flavor_id} and {flavor.flavor_id} share {location}; "
                "subtract the selected Flavor first",
            )
        if selected_flavor is not None:
            assert slot_id is not None
            selected[flavor.flavor_id] = replace(
                selected_flavor,
                slot_ids=tuple(sorted((*selected_flavor.slot_ids, slot_id))),
            )
            preference_locations.difference_update(selection_locations(bound))
            continue
        conflict = next(
            (other for other in selected.values() if _flavors_conflict(bound, other)),
            None,
        )
        if conflict is not None:
            conflict_locations = selection_locations(conflict)
            if conflict_locations.issubset(preference_locations):
                selected.pop(conflict.flavor_id)
                preference_locations.difference_update(conflict_locations)
                conflict = None
        if conflict is not None:
            raise FlavorSelectionError(
                "flavor_selection.mutually_exclusive_flavors",
                f"{conflict.flavor_id} conflicts with {flavor.flavor_id}; "
                "subtract the selected Flavor first",
            )
        selected[flavor.flavor_id] = bound
        preference_locations.difference_update(selection_locations(bound))
    final = tuple(selected.values())
    for index, flavor in enumerate(final):
        for other in final[index + 1 :]:
            if same_selection_location(flavor, other):
                raise FlavorSelectionError(
                    "flavor_selection.mutually_exclusive_flavors",
                    f"{flavor.flavor_id} and {other.flavor_id} share a selection "
                    "location that no selector resolved",
                )
            if _flavors_conflict(flavor, other):
                raise FlavorSelectionError(
                    "flavor_selection.mutually_exclusive_flavors",
                    f"{flavor.flavor_id} conflicts with {other.flavor_id}",
                )
    return tuple(
        sorted(
            final,
            key=lambda item: (item.axis, item.slot_ids, item.flavor_id),
        )
    )


def _flavors_conflict(
    left: FlavorSelectionCandidate, right: FlavorSelectionCandidate
) -> bool:
    return bool(
        left.reference_ids.intersection(right.conflicts)
        or right.reference_ids.intersection(left.conflicts)
    )


__all__ = [
    "flavor_candidate_aliases",
    "parse_flavor_selector_body",
    "FlavorSelectionError",
    "apply_flavor_selectors",
]
