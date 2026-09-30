"""Typed, policy-driven Flavor resolution and effective-revision derivation."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.contracts import (
    CandidateStatus,
    ComponentRevision,
    ContributionReference,
    EffectiveRevision,
    EffectiveSpecificationSet,
    FlavorAxis,
    FlavorCandidateDecision,
    FlavorCardinality,
    FlavorSetLock,
    FlavorSlot,
    MergeOperator,
    TargetProfile,
    VersionedContentRef,
)
from literate_ai.registry import DescriptorRegistry, FlavorDescriptor

from .components import CompositionError
from .policy import (
    FlavorSlotSelectionPolicy,
    SelectionError,
    SelectionPolicy,
    UniqueSelectionPolicy,
)


@dataclass(frozen=True, slots=True)
class FlavorResolution:
    lock: FlavorSetLock
    effective_specifications: EffectiveSpecificationSet
    effective_revision: EffectiveRevision


class FlavorResolver:
    def __init__(
        self,
        registry: DescriptorRegistry,
        policy: SelectionPolicy[FlavorDescriptor] | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy or UniqueSelectionPolicy()

    def resolve(
        self, base_revision: ComponentRevision, target_profile: TargetProfile
    ) -> FlavorResolution:
        slots = base_revision.definition.flavor_slots
        slots_by_id = {slot.slot_id: slot for slot in slots}
        axis_slots: dict[FlavorAxis, list[FlavorSlot]] = {}
        for slot in slots:
            axis_slots.setdefault(slot.axis, []).append(slot)
        axis_constraints = {
            item.axis: item
            for item in target_profile.constraints
            if item.slot_id is None
        }
        slot_constraints = {
            item.slot_id: item
            for item in target_profile.constraints
            if item.slot_id is not None
        }
        unknown_slots = sorted(set(slot_constraints) - set(slots_by_id))
        if unknown_slots:
            raise CompositionError(
                "undeclared-target-slot",
                f"base Component has no Flavor slot for: {', '.join(unknown_slots)}",
            )
        for slot_id, constraint in slot_constraints.items():
            slot = slots_by_id[slot_id]
            if constraint.axis is not slot.axis:
                raise CompositionError(
                    "target-slot-axis-mismatch",
                    f"target slot {slot_id} uses {constraint.axis.value}, not "
                    f"{slot.axis.value}",
                )
        duplicated_axis_targets = sorted(
            axis.value
            for axis, constraint in axis_constraints.items()
            if constraint is not None and len(axis_slots.get(axis, ())) > 1
        )
        if duplicated_axis_targets:
            raise CompositionError(
                "ambiguous-target-axis",
                "target axes with multiple slots require slot-scoped constraints: "
                + ", ".join(duplicated_axis_targets),
            )
        slot_axes = {slot.axis for slot in slots}
        undeclared = [axis.value for axis in axis_constraints if axis not in slot_axes]
        if undeclared:
            raise CompositionError(
                "undeclared-target-axis",
                f"base Component has no Flavor slot for: {', '.join(undeclared)}",
            )

        selected: list[FlavorDescriptor] = []
        decisions: dict[str, FlavorCandidateDecision] = {}
        base_capabilities = {item.name for item in base_revision.definition.provides}

        def record_decision(
            descriptor: FlavorDescriptor,
            status: CandidateStatus,
            reasons: tuple[str, ...],
        ) -> None:
            """Aggregate a candidate considered by more than one role slot.

            Candidate decisions describe the resolved Flavor set, not every
            intermediate slot comparison. Selection for any slot therefore wins
            over rejection elsewhere; rejection reasons are retained only when
            the Flavor was never selected.
            """

            key = descriptor.revision_identity.uri
            current = decisions.get(key)
            if current is not None and current.status is CandidateStatus.SELECTED:
                return
            if status is CandidateStatus.SELECTED:
                decisions[key] = FlavorCandidateDecision(
                    descriptor.revision_identity, status, ()
                )
                return
            combined = reasons
            if current is not None:
                combined = tuple(dict.fromkeys((*current.reasons, *reasons)))
            decisions[key] = FlavorCandidateDecision(
                descriptor.revision_identity, status, combined
            )

        for slot in slots:
            eligible: list[FlavorDescriptor] = []
            target = slot_constraints.get(slot.slot_id) or axis_constraints.get(
                slot.axis
            )
            for descriptor in self._registry.flavors():
                if descriptor.definition.primary_axis is not slot.axis:
                    continue
                reasons: list[str] = []
                if not descriptor.is_available:
                    reasons.extend(
                        item.code for item in descriptor.availability_reasons
                    )
                if target is not None and descriptor.axis_value != target.value:
                    reasons.append(
                        f"axis value {descriptor.axis_value!r} does not match "
                        f"{target.value!r}"
                    )
                required_base = set(descriptor.definition.applicable_capabilities)
                if not required_base <= base_capabilities:
                    reasons.append(
                        "base Component does not provide all applicability capabilities"
                    )
                for secondary in descriptor.definition.secondary_constraints:
                    actual = axis_constraints.get(secondary.axis)
                    if actual is None and secondary.optional:
                        continue
                    if actual is None or actual.value != secondary.value:
                        reasons.append(
                            "secondary target constraint "
                            f"{secondary.axis.value}={secondary.value} is not satisfied"
                        )
                if reasons:
                    record_decision(
                        descriptor,
                        CandidateStatus.REJECTED,
                        tuple(reasons),
                    )
                else:
                    eligible.append(descriptor)

            chosen: tuple[FlavorDescriptor, ...] = ()
            if eligible:
                try:
                    subject = f"Flavor slot {slot.slot_id}"
                    if isinstance(self._policy, FlavorSlotSelectionPolicy):
                        chosen = self._policy.choose_for_slot(
                            tuple(eligible),
                            slot_id=slot.slot_id,
                            subject=subject,
                        )
                    else:
                        chosen = self._policy.choose(tuple(eligible), subject=subject)
                except SelectionError as error:
                    raise CompositionError(error.code, str(error)) from error
                eligible_ids = {id(item) for item in eligible}
                chosen_ids = tuple(id(item) for item in chosen)
                if len(set(chosen_ids)) != len(chosen_ids) or any(
                    item_id not in eligible_ids for item_id in chosen_ids
                ):
                    raise CompositionError(
                        "invalid-policy-result",
                        "Flavor policy must select unique eligible candidates",
                    )
            self._check_cardinality(
                slot,
                len(chosen),
                target_required=target is not None and not target.optional,
            )
            for descriptor in eligible:
                if descriptor in chosen:
                    if descriptor not in selected:
                        selected.append(descriptor)
                    record_decision(
                        descriptor,
                        CandidateStatus.SELECTED,
                        (),
                    )
                else:
                    record_decision(
                        descriptor,
                        CandidateStatus.REJECTED,
                        ("eligible but not selected by explicit policy",),
                    )

        self._check_selected_relationships(selected)
        ordered = self._order(selected)
        contributions = self._merge_contributions(ordered)
        lock = FlavorSetLock(
            base_revision.identity,
            target_profile,
            self._policy.identity,
            tuple(decisions.values()),
            tuple(item.revision_identity for item in ordered),
            base_revision.ref,
            tuple(
                VersionedContentRef(
                    "flavor",
                    item.definition.coordinate.uri,
                    item.definition.version,
                    item.revision_identity,
                )
                for item in ordered
            ),
        )
        specification_fragments = tuple(
            reference.identity
            for descriptor in ordered
            for reference in descriptor.definition.specification_fragments
        )
        effective_specifications = EffectiveSpecificationSet(
            base_revision.specifications.identity,
            specification_fragments,
            lock.identity,
        )
        effective_revision = EffectiveRevision(
            base_revision.identity,
            target_profile.identity,
            lock.identity,
            effective_specifications.identity,
            contributions,
            self._policy.identity,
        )
        return FlavorResolution(lock, effective_specifications, effective_revision)

    @staticmethod
    def _check_cardinality(
        slot: FlavorSlot, count: int, *, target_required: bool
    ) -> None:
        if target_required and count == 0:
            raise CompositionError(
                "flavor-target-unsatisfied",
                f"slot {slot.slot_id} did not satisfy its required target constraint",
            )
        if slot.cardinality is FlavorCardinality.EXACTLY_ONE and count != 1:
            raise CompositionError(
                "flavor-cardinality", f"slot {slot.slot_id} requires exactly one Flavor"
            )
        if slot.cardinality is FlavorCardinality.ZERO_OR_ONE and count > 1:
            raise CompositionError(
                "flavor-cardinality", f"slot {slot.slot_id} permits at most one Flavor"
            )
        if slot.cardinality is FlavorCardinality.ONE_OR_MORE and count < 1:
            raise CompositionError(
                "flavor-cardinality",
                f"slot {slot.slot_id} requires one or more Flavors",
            )
        if slot.cardinality is FlavorCardinality.BOUNDED:
            assert slot.minimum is not None and slot.maximum is not None
            if not slot.minimum <= count <= slot.maximum:
                raise CompositionError(
                    "flavor-cardinality",
                    f"slot {slot.slot_id} requires "
                    f"{slot.minimum}..{slot.maximum} Flavors",
                )

    @staticmethod
    def _check_selected_relationships(selected: list[FlavorDescriptor]) -> None:
        coordinates = {item.coordinate for item in selected}
        for descriptor in selected:
            conflicts = coordinates & set(descriptor.definition.conflicts)
            if conflicts:
                raise CompositionError(
                    "flavor-conflict",
                    f"{descriptor.coordinate} conflicts with {', '.join(conflicts)}",
                )
            missing = set(descriptor.definition.co_requisites) - coordinates
            if missing:
                raise CompositionError(
                    "missing-flavor-corequisite",
                    f"{descriptor.coordinate} requires {', '.join(missing)}",
                )
            for group in descriptor.definition.co_requisite_groups:
                matches = coordinates.intersection(group.alternatives)
                if len(matches) != 1:
                    detail = (
                        "none selected" if not matches else ", ".join(sorted(matches))
                    )
                    raise CompositionError(
                        "flavor-corequisite-group-unsatisfied",
                        f"{descriptor.coordinate} requires exactly one realization "
                        f"from {group.group_id}; {detail}",
                    )

    @staticmethod
    def _order(selected: list[FlavorDescriptor]) -> tuple[FlavorDescriptor, ...]:
        by_coordinate = {item.coordinate: item for item in selected}
        incoming: dict[str, set[str]] = {item.coordinate: set() for item in selected}
        for item in selected:
            for before in item.definition.order_before:
                if before in by_coordinate:
                    incoming[before].add(item.coordinate)
            for after in item.definition.order_after:
                if after in by_coordinate:
                    incoming[item.coordinate].add(after)
        remaining = list(selected)
        ordered: list[FlavorDescriptor] = []
        while remaining:
            ready = next(
                (item for item in remaining if not incoming[item.coordinate]), None
            )
            if ready is None:
                raise CompositionError(
                    "flavor-order-cycle", "Flavor ordering contains a cycle"
                )
            ordered.append(ready)
            remaining.remove(ready)
            for dependencies in incoming.values():
                dependencies.discard(ready.coordinate)
        return tuple(ordered)

    @staticmethod
    def _merge_contributions(
        selected: tuple[FlavorDescriptor, ...],
    ) -> tuple[ContributionReference, ...]:
        merged: list[ContributionReference] = []
        by_id: dict[str, ContributionReference] = {}
        slot_operators: dict[tuple[str, str], MergeOperator] = {}
        slot_content: dict[tuple[str, str], set[str]] = {}
        occupied_slots: set[tuple[str, str]] = set()
        for descriptor in selected:
            for contribution in descriptor.definition.contributions:
                existing_id = by_id.get(contribution.contribution_id)
                if existing_id is not None:
                    if (
                        existing_id.kind is contribution.kind
                        and existing_id.merge_operator is contribution.merge_operator
                        and existing_id.slot == contribution.slot
                        and existing_id.content.identity
                        == contribution.content.identity
                    ):
                        continue
                    raise CompositionError(
                        "duplicate-contribution-id",
                        "non-identical contributions use duplicate ID "
                        f"{contribution.contribution_id}",
                    )
                by_id[contribution.contribution_id] = contribution
                key = (contribution.kind.value, contribution.slot)
                existing_operator = slot_operators.get(key)
                if (
                    existing_operator is not None
                    and existing_operator is not contribution.merge_operator
                ):
                    raise CompositionError(
                        "flavor-contribution-conflict",
                        "contributions disagree on the merge operator for "
                        f"{key[0]}:{key[1]}",
                    )
                slot_operators[key] = contribution.merge_operator
                content_identity = contribution.content.identity.uri

                if contribution.merge_operator is MergeOperator.EXACT_SINGLETON:
                    identities = slot_content.setdefault(key, set())
                    if content_identity in identities:
                        continue
                    if identities:
                        raise CompositionError(
                            "flavor-contribution-conflict",
                            "non-identical singleton contributions for "
                            f"{key[0]}:{key[1]}",
                        )
                    identities.add(content_identity)
                elif contribution.merge_operator is MergeOperator.ADDITIVE_SET:
                    identities = slot_content.setdefault(key, set())
                    if content_identity in identities:
                        continue
                    identities.add(content_identity)
                elif contribution.merge_operator is MergeOperator.KEYED_UNION:
                    identities = slot_content.setdefault(key, set())
                    if content_identity in identities:
                        continue
                    identities.add(content_identity)
                elif contribution.merge_operator is MergeOperator.EXPLICIT_CONFLICT:
                    if key in occupied_slots:
                        raise CompositionError(
                            "flavor-contribution-conflict",
                            "explicitly conflicting contributions for "
                            f"{key[0]}:{key[1]}",
                        )
                occupied_slots.add(key)
                merged.append(contribution)
        return tuple(merged)


__all__ = ["FlavorResolution", "FlavorResolver"]
