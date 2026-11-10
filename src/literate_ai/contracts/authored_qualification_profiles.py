"""Authored qualification selections; no source baseline or execution grant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail, int_value, list_value, string_value
from .identity import ComponentCoordinate, ContentIdentity, contract_identity

AUTHORED_QUALIFICATION_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:authored-qualification-profile"
)
AUTHORED_QUALIFICATION_ORACLE_SELECTION_SCHEMA = (
    "urn:literate-ai:schema:v2:authored-qualification-oracle-selection"
)


@dataclass(frozen=True, slots=True)
class AuthoredQualificationOracleSelection:
    """Pin an independently reviewed oracle for one exact logical Component.

    The current reader must join this selection to the actual locked revision,
    selected public interfaces, import surface and original loaded verifier.
    A pin alone establishes neither human review nor successful acceptance.
    """

    component: ComponentCoordinate
    oracle_identity: ContentIdentity

    SCHEMA: ClassVar[str] = AUTHORED_QUALIFICATION_ORACLE_SELECTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.component, ComponentCoordinate):
            fail("AuthoredQualificationOracleSelection.component", "must be typed")
        if not isinstance(self.oracle_identity, ContentIdentity):
            fail(
                "AuthoredQualificationOracleSelection.oracle_identity", "must be typed"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component": self.component.to_dict(),
            "oracle_identity": self.oracle_identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> AuthoredQualificationOracleSelection:
        data = contract_fields(
            value,
            path="AuthoredQualificationOracleSelection",
            schema_uri=cls.SCHEMA,
            required=frozenset({"component", "oracle_identity"}),
        )
        return cls(
            ComponentCoordinate.from_dict(data["component"]),
            ContentIdentity.from_dict(data["oracle_identity"]),
        )


@dataclass(frozen=True, slots=True)
class AuthoredQualificationProfile:
    """Select every graph oracle before generation without copying its answers."""

    profile_id: str
    oracles: tuple[AuthoredQualificationOracleSelection, ...]
    minimum_clean_runs: int = 2

    SCHEMA: ClassVar[str] = AUTHORED_QUALIFICATION_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        text = string_value(self.profile_id, "AuthoredQualificationProfile.profile_id")
        if text != text.strip():
            fail("AuthoredQualificationProfile.profile_id", "must be canonical text")
        int_value(
            self.minimum_clean_runs,
            "AuthoredQualificationProfile.minimum_clean_runs",
            minimum=2,
        )
        if (
            not isinstance(self.oracles, tuple)
            or not self.oracles
            or any(
                not isinstance(item, AuthoredQualificationOracleSelection)
                for item in self.oracles
            )
        ):
            fail(
                "AuthoredQualificationProfile.oracles", "must be a nonempty typed tuple"
            )
        keys = tuple(item.component.uri for item in self.oracles)
        if keys != tuple(sorted(set(keys))):
            fail(
                "AuthoredQualificationProfile.oracles",
                "must select each Component once in canonical coordinate order",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "profile_id": self.profile_id,
            "oracles": [item.to_dict() for item in self.oracles],
            "minimum_clean_runs": self.minimum_clean_runs,
        }

    @classmethod
    def from_dict(cls, value: object) -> AuthoredQualificationProfile:
        data = contract_fields(
            value,
            path="AuthoredQualificationProfile",
            schema_uri=cls.SCHEMA,
            required=frozenset({"profile_id", "oracles", "minimum_clean_runs"}),
        )
        return cls(
            data["profile_id"],
            tuple(
                AuthoredQualificationOracleSelection.from_dict(item)
                for item in list_value(data["oracles"], "oracles")
            ),
            data["minimum_clean_runs"],
        )
