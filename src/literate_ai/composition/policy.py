"""Explicit provider and Flavor selection policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.registry import ComponentDescriptor, FlavorDescriptor


class SelectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


T = TypeVar("T", ComponentDescriptor, FlavorDescriptor)


class SelectionPolicy(Protocol[T]):
    @property
    def identity(self) -> ContentIdentity: ...

    def choose(self, candidates: tuple[T, ...], *, subject: str) -> tuple[T, ...]: ...


@runtime_checkable
class FlavorSlotSelectionPolicy(Protocol):
    """Optional policy extension for exact Component-slot selection."""

    def choose_for_slot(
        self,
        candidates: tuple[FlavorDescriptor, ...],
        *,
        slot_id: str,
        subject: str,
    ) -> tuple[FlavorDescriptor, ...]: ...


@dataclass(frozen=True, slots=True)
class UniqueSelectionPolicy:
    """Select only when the eligible set itself is unambiguous."""

    policy_id: str = "policy://literate-ai/unique@1"

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity({"policy": self.policy_id})

    def choose(self, candidates: tuple[T, ...], *, subject: str) -> tuple[T, ...]:
        if not candidates:
            raise SelectionError("no-provider", f"no eligible provider for {subject}")
        if len(candidates) != 1:
            raise SelectionError(
                "ambiguous-provider",
                f"{len(candidates)} eligible providers for {subject}; "
                "an explicit policy is required",
            )
        return (candidates[0],)


@dataclass(frozen=True, slots=True)
class PreferredSelectionPolicy:
    """Choose by an explicit ordered list of exact revision IDs or coordinates."""

    preferences: tuple[str, ...]
    policy_id: str = "policy://literate-ai/preferred@1"

    def __post_init__(self) -> None:
        if not self.preferences or len(set(self.preferences)) != len(self.preferences):
            raise ValueError("preferences must be a non-empty unique ordered list")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {"policy": self.policy_id, "preferences": list(self.preferences)}
        )

    def choose(self, candidates: tuple[T, ...], *, subject: str) -> tuple[T, ...]:
        for preference in self.preferences:
            exact = tuple(
                item for item in candidates if item.revision_identity.uri == preference
            )
            if len(exact) == 1:
                return exact
            coordinates = tuple(
                item for item in candidates if item.coordinate == preference
            )
            if len(coordinates) == 1:
                return coordinates
            if len(coordinates) > 1:
                raise SelectionError(
                    "ambiguous-preference",
                    f"preference {preference!r} matches multiple revisions "
                    f"for {subject}",
                )
        raise SelectionError(
            "no-preferred-provider",
            f"no eligible provider for {subject} matches policy",
        )


@dataclass(frozen=True, slots=True)
class ExplicitFlavorSelectionPolicy:
    """Select the exact Flavor revisions named by a resolved selector set.

    An empty result is intentional: the slot cardinality, rather than the policy,
    decides whether a slot may remain unselected.  This keeps ``+flavor``/``-flavor``
    selection distinct from catalog eligibility and makes multi-valued slots possible.
    """

    revision_ids: tuple[str, ...]
    slot_revision_ids: tuple[tuple[str, str], ...] = ()
    policy_id: str = "policy://literate-ai/explicit-flavor-set@1"

    def __post_init__(self) -> None:
        if len(set(self.revision_ids)) != len(self.revision_ids) or any(
            not item for item in self.revision_ids
        ):
            raise ValueError("explicit Flavor revisions must be unique non-empty IDs")
        for revision_id in self.revision_ids:
            ContentIdentity.parse_uri(revision_id)
        if len(set(self.slot_revision_ids)) != len(self.slot_revision_ids) or any(
            not slot_id for slot_id, _revision_id in self.slot_revision_ids
        ):
            raise ValueError(
                "explicit Flavor slot bindings must be unique and non-empty"
            )
        selected = set(self.revision_ids)
        for _slot_id, revision_id in self.slot_revision_ids:
            ContentIdentity.parse_uri(revision_id)
            if revision_id not in selected:
                raise ValueError(
                    "explicit Flavor slot bindings must name selected revisions"
                )

    @property
    def identity(self) -> ContentIdentity:
        value: dict[str, object] = {
            "policy": self.policy_id,
            "revision_ids": list(self.revision_ids),
        }
        if self.slot_revision_ids:
            value["slot_revision_ids"] = [
                {"slot_id": slot_id, "revision_id": revision_id}
                for slot_id, revision_id in self.slot_revision_ids
            ]
        return canonical_identity(value)

    def choose(
        self, candidates: tuple[FlavorDescriptor, ...], *, subject: str
    ) -> tuple[FlavorDescriptor, ...]:
        del subject
        by_revision = {item.revision_identity.uri: item for item in candidates}
        return tuple(
            by_revision[revision_id]
            for revision_id in self.revision_ids
            if revision_id in by_revision
        )

    def choose_for_slot(
        self,
        candidates: tuple[FlavorDescriptor, ...],
        *,
        slot_id: str,
        subject: str,
    ) -> tuple[FlavorDescriptor, ...]:
        """Choose only revisions bound to this slot, without duplicating content."""

        del subject
        by_revision = {item.revision_identity.uri: item for item in candidates}
        bound_revision_ids = tuple(
            revision_id
            for binding_slot_id, revision_id in self.slot_revision_ids
            if binding_slot_id == slot_id
        )
        if not bound_revision_ids:
            revisions_bound_elsewhere = {
                revision_id for _binding_slot_id, revision_id in self.slot_revision_ids
            }
            bound_revision_ids = tuple(
                revision_id
                for revision_id in self.revision_ids
                if revision_id not in revisions_bound_elsewhere
            )
        return tuple(
            by_revision[revision_id]
            for revision_id in bound_revision_ids
            if revision_id in by_revision
        )


__all__ = [
    "ExplicitFlavorSelectionPolicy",
    "FlavorSlotSelectionPolicy",
    "PreferredSelectionPolicy",
    "SelectionError",
    "SelectionPolicy",
    "UniqueSelectionPolicy",
]
