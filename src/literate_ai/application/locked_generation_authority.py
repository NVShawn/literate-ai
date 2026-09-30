"""Pure projection from a canonical Component lock to generation authority."""

from __future__ import annotations

from dataclasses import dataclass, replace

from literate_ai.application.flavor_selection import (
    FlavorSelectionError,
    apply_flavor_selectors,
    flavor_candidate_aliases,
    parse_flavor_selector_body,
    parse_scoped_flavor_selector,
)
from literate_ai.contracts.component_locking import ComponentAuthoring, ComponentLock
from literate_ai.contracts.flavors import (
    FlavorCardinality,
    FlavorRevision,
    FlavorSelectionCandidate,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


class LockedGenerationAuthorityError(ValueError):
    """A lock cannot authorize the requested target and Flavor selection."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LockedGenerationAuthority:
    """Provider-neutral, selected-only authority for one generation operation."""

    lock: ComponentLock
    root_authoring: ComponentAuthoring
    authorings: tuple[ComponentAuthoring, ...]
    selected_flavors: tuple[FlavorRevision, ...]
    requested_flavor_selectors: tuple[str, ...]

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/locked-generation-authority@1",
            "component_lock_identity": self.lock.identity.uri,
            "root_authoring_identity": self.root_authoring.identity.uri,
            "authoring_identities": [item.identity.uri for item in self.authorings],
            "selected_flavor_revisions": [
                item.identity.uri for item in self.selected_flavors
            ],
            "target_name": self.lock.target_name,
            "target_profile_identity": self.lock.target_profile_identity.uri,
            "selection_policy_identity": self.lock.selection_policy_identity.uri,
            "requested_flavor_selectors": list(self.requested_flavor_selectors),
        }


def _selector_parts(
    selector: str,
) -> tuple[str | None, str, str | None, str, str]:
    if not isinstance(selector, str):
        raise LockedGenerationAuthorityError(
            "locked_generation.selector_invalid",
            "Flavor selectors must begin with + or -",
        )
    try:
        coordinate, effective = parse_scoped_flavor_selector(selector)
        if len(effective) < 2 or effective[0] not in "+-":
            raise FlavorSelectionError(
                "flavor_selection.invalid_flavor_selector",
                "Flavor selectors must begin with + or -",
            )
        slot_id, alias = parse_flavor_selector_body(effective[1:])
    except FlavorSelectionError as exc:
        raise LockedGenerationAuthorityError(
            "locked_generation.selector_invalid", exc.message
        ) from exc
    return coordinate, effective[0], slot_id, alias, effective


def _assert_flavor_selectors(
    lock: ComponentLock,
    *,
    flavor_catalog: tuple[FlavorRevision, ...],
    selectors: tuple[str, ...],
) -> None:
    aliases: dict[str, list[FlavorRevision]] = {}
    for flavor in flavor_catalog:
        definition = flavor.definition
        candidate = FlavorSelectionCandidate(
            candidate_identity=flavor.identity.uri,
            flavor_id=definition.coordinate.name,
            axis=definition.primary_axis.value,
            value=definition.supported_targets[0],
            conflicts=definition.conflicts,
            coordinate_uri=definition.coordinate.uri,
        )
        for alias in flavor_candidate_aliases(candidate):
            aliases.setdefault(alias, []).append(flavor)

    parsed_selectors = tuple(_selector_parts(selector) for selector in selectors)
    for (
        _coordinate,
        _operation,
        _selected_slot_id,
        alias,
        _effective,
    ) in parsed_selectors:
        candidates = aliases.get(alias, [])
        if not candidates:
            raise LockedGenerationAuthorityError(
                "locked_generation.selector_unknown",
                f"Flavor selector is absent from the current catalog: {alias!r}",
            )
        revisions = {item.identity.uri for item in candidates}
        if len(revisions) != 1:
            raise LockedGenerationAuthorityError(
                "locked_generation.selector_ambiguous",
                f"Flavor selector is ambiguous in the current catalog: {alias!r}",
            )

    consumed: set[int] = set()
    for node in lock.nodes:
        slots = tuple(item.slot for item in node.target_flavor_selection.slots)
        slot_axes = {item.slot_id: item.axis.value for item in slots}
        eligible_revisions = tuple(
            item
            for item in flavor_catalog
            if any(
                slot.axis is item.definition.primary_axis
                and (
                    not item.definition.applicable_capabilities
                    or slot.capability_contract
                    in item.definition.applicable_capabilities
                )
                for slot in slots
            )
        )
        candidates = tuple(
            FlavorSelectionCandidate(
                candidate_identity=item.identity.uri,
                flavor_id=item.definition.coordinate.name,
                axis=item.definition.primary_axis.value,
                value=item.definition.supported_targets[0],
                conflicts=item.definition.conflicts,
                coordinate_uri=item.definition.coordinate.uri,
            )
            for item in eligible_revisions
        )
        candidate_by_identity = {item.candidate_identity: item for item in candidates}
        selected_slots: dict[str, set[str]] = {}
        for slot_resolution in node.target_flavor_selection.slots:
            for selected in slot_resolution.selected:
                selected_slots.setdefault(selected.flavor_revision.uri, set()).add(
                    slot_resolution.slot.slot_id
                )
        initial = tuple(
            replace(candidate_by_identity[identity], slot_ids=tuple(sorted(slot_ids)))
            for identity, slot_ids in sorted(selected_slots.items())
        )
        eligible_aliases = {
            alias for item in candidates for alias in flavor_candidate_aliases(item)
        }
        applicable_selectors: list[str] = []
        for index, (_selector, parsed) in enumerate(
            zip(selectors, parsed_selectors, strict=True)
        ):
            coordinate, _operation, selected_slot_id, alias, effective = parsed
            if coordinate is not None and coordinate != node.revision.coordinate.uri:
                continue
            if alias not in eligible_aliases:
                continue
            if selected_slot_id is not None and selected_slot_id not in slot_axes:
                continue
            consumed.add(index)
            applicable_selectors.append(effective)
        multi_slots = frozenset(
            item.slot_id
            for item in slots
            if item.cardinality is FlavorCardinality.ONE_OR_MORE
            or (
                item.cardinality is FlavorCardinality.BOUNDED
                and item.maximum is not None
                and item.maximum > 1
            )
        )
        multi_axes = frozenset(
            item.axis.value for item in slots if item.slot_id in multi_slots
        )
        try:
            replayed = apply_flavor_selectors(
                candidates,
                applicable_selectors,
                initial=initial,
                initial_is_preferences=True,
                multi_value_axes=multi_axes,
                slot_axes=slot_axes,
                multi_value_slots=multi_slots,
            )
        except FlavorSelectionError as exc:
            raise LockedGenerationAuthorityError(
                "locked_generation.selector_mismatch", exc.message
            ) from exc

        def signature(
            values: tuple[FlavorSelectionCandidate, ...],
        ) -> tuple[tuple[str, tuple[str, ...]], ...]:
            return tuple(
                sorted((item.candidate_identity, item.slot_ids) for item in values)
            )

        if signature(replayed) != signature(initial):
            raise LockedGenerationAuthorityError(
                "locked_generation.selector_mismatch",
                "Component lock does not represent the requested Flavor selectors",
            )
    if consumed != set(range(len(selectors))):
        missing_index = next(
            index for index in range(len(selectors)) if index not in consumed
        )
        raise LockedGenerationAuthorityError(
            "locked_generation.selector_noop",
            "Flavor selector has no applicable reachable slot: "
            f"{selectors[missing_index]!r}",
        )


def project_locked_generation_authority(
    lock: ComponentLock,
    *,
    root_authoring: ComponentAuthoring,
    authorings: tuple[ComponentAuthoring, ...],
    flavor_catalog: tuple[FlavorRevision, ...],
    target_name: str,
    flavor_selectors: tuple[str, ...] = (),
) -> LockedGenerationAuthority:
    """Assert current typed inputs and project one selected-only generation view."""

    if not isinstance(lock, ComponentLock):
        raise TypeError("locked generation projection requires a ComponentLock")
    if lock.target_name != target_name:
        raise LockedGenerationAuthorityError(
            "locked_generation.target_mismatch",
            f"Component lock targets {lock.target_name!r}, not {target_name!r}",
        )
    authoring_by_identity = {item.identity.uri: item for item in authorings}
    if len(authoring_by_identity) != len(authorings):
        raise LockedGenerationAuthorityError(
            "locked_generation.authoring_ambiguous",
            "current Component catalog repeats an authoring identity",
        )
    locked_authoring_identities = {
        item.revision.authoring_identity.uri for item in lock.nodes
    }
    if set(authoring_by_identity) != locked_authoring_identities:
        raise LockedGenerationAuthorityError(
            "locked_generation.authoring_stale",
            "Component lock does not bind every and only current selected authoring",
        )
    root_node = next(
        item for item in lock.nodes if item.revision.identity == lock.root_revision
    )
    if root_node.revision.authoring_identity != root_authoring.identity:
        raise LockedGenerationAuthorityError(
            "locked_generation.root_mismatch",
            "Component lock root does not identify the requested Component authoring",
        )

    flavors_by_identity = {item.identity.uri: item for item in flavor_catalog}
    if len(flavors_by_identity) != len(flavor_catalog):
        raise LockedGenerationAuthorityError(
            "locked_generation.flavor_ambiguous",
            "current Flavor catalog repeats a revision identity",
        )
    selected_identities = {
        identity.uri
        for node in lock.nodes
        for identity in node.revision.selected_flavor_revisions
    }
    missing_flavors = selected_identities - set(flavors_by_identity)
    if missing_flavors:
        raise LockedGenerationAuthorityError(
            "locked_generation.flavor_stale",
            "Component lock selects a Flavor revision absent from the current catalog",
        )
    for node in lock.nodes:
        for slot_resolution in node.target_flavor_selection.slots:
            slot = slot_resolution.slot
            for selected in slot_resolution.selected:
                definition = flavors_by_identity[
                    selected.flavor_revision.uri
                ].definition
                if (
                    definition.primary_axis is not slot.axis
                    or definition.supported_targets != (selected.value,)
                    or (
                        definition.applicable_capabilities
                        and slot.capability_contract
                        not in definition.applicable_capabilities
                    )
                ):
                    raise LockedGenerationAuthorityError(
                        "locked_generation.flavor_binding_invalid",
                        "locked Flavor revision does not satisfy its Component slot",
                    )

    _assert_flavor_selectors(
        lock,
        flavor_catalog=flavor_catalog,
        selectors=flavor_selectors,
    )
    selected_flavors = tuple(
        flavors_by_identity[identity] for identity in sorted(selected_identities)
    )
    ordered_authorings = tuple(sorted(authorings, key=lambda item: item.identity.uri))
    return LockedGenerationAuthority(
        lock,
        root_authoring,
        ordered_authorings,
        selected_flavors,
        flavor_selectors,
    )


__all__ = [
    "LockedGenerationAuthority",
    "LockedGenerationAuthorityError",
    "project_locked_generation_authority",
]
