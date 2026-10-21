"""Input ownership references canonical Components; it does not define a catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .component_locking import ComponentAuthoring
from .identity import ComponentCoordinate, ContentIdentity
from .paths import canonical_relative_posix_path


@dataclass(frozen=True, slots=True)
class RetainedComponentOwner:
    """The most specific declared prefix owns an input, including empty dirs."""

    prefix: str
    component: ComponentCoordinate
    authoring_identity: ContentIdentity

    def __post_init__(self) -> None:
        canonical_relative_posix_path(self.prefix, label="retained ownership prefix")
        if not isinstance(self.component, ComponentCoordinate) or not isinstance(
            self.authoring_identity, ContentIdentity
        ):
            raise TypeError("Retained ownership requires typed Component references")

    def to_dict(self) -> dict[str, object]:
        return {
            "prefix": self.prefix,
            "component": self.component.to_dict(),
            "authoring_identity": self.authoring_identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> RetainedComponentOwner:
        if not isinstance(value, dict) or set(value) != {
            "prefix",
            "component",
            "authoring_identity",
        }:
            raise ValueError("Invalid retained Component ownership fields")
        return cls(
            value["prefix"],
            ComponentCoordinate.from_dict(value["component"]),
            ContentIdentity.from_dict(value["authoring_identity"]),
        )


def retained_owner(
    name: str, by_prefix: dict[str, RetainedComponentOwner]
) -> RetainedComponentOwner | None:
    """Resolve by path depth, never by scanning the whole ownership inventory."""
    current = name
    while current:
        if current in by_prefix:
            return by_prefix[current]
        current = current.rpartition("/")[0]
    return None


def validate_retained_ownership(
    owners: tuple[RetainedComponentOwner, ...], nodes: set[str]
) -> None:
    """Validate complete deterministic coverage, without claiming registration."""
    if type(owners) is not tuple or any(
        not isinstance(owner, RetainedComponentOwner) for owner in owners
    ):
        raise TypeError("Typed retained Component ownership tuple required")
    prefixes = tuple(owner.prefix for owner in owners)
    if prefixes != tuple(sorted(prefixes)) or len(set(prefixes)) != len(prefixes):
        raise ValueError("Retained ownership prefixes must be unique and ordered")
    if len({prefix.casefold() for prefix in prefixes}) != len(prefixes):
        raise ValueError("Retained ownership prefixes alias on a supported filesystem")
    if not set(prefixes) <= nodes:
        raise ValueError("Retained ownership prefix is absent from the input inventory")
    definitions: dict[str, ContentIdentity] = {}
    for owner in owners:
        existing = definitions.setdefault(owner.component.uri, owner.authoring_identity)
        if existing != owner.authoring_identity:
            raise ValueError(
                "Retained ownership gives one Component conflicting authorings"
            )
    by_prefix = {owner.prefix: owner for owner in owners}
    used: set[str] = set()
    for name in nodes:
        owner = retained_owner(name, by_prefix)
        if owner is None:
            raise ValueError("Retained input has no canonical Component owner: " + name)
        used.add(owner.prefix)
    if used != set(prefixes):
        raise ValueError("Retained ownership prefix owns no input")


def require_registered_retained_owners(
    owners: tuple[RetainedComponentOwner, ...],
    authorings: tuple[ComponentAuthoring, ...],
) -> tuple[ContentIdentity, ...]:
    """Join exact references to the real catalog; no registration is synthesized."""
    if type(authorings) is not tuple or any(
        not isinstance(item, ComponentAuthoring) for item in authorings
    ):
        raise TypeError("Canonical authored Component catalog required")
    known = {(item.coordinate.uri, item.identity) for item in authorings}
    for owner in owners:
        if (owner.component.uri, owner.authoring_identity) not in known:
            raise ValueError(
                "retained.ownership.component-missing-or-stale: " + owner.component.uri
            )
    return tuple(
        sorted({owner.authoring_identity for owner in owners}, key=lambda i: i.uri)
    )
