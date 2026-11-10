"""Closed qualification provenance references (ADR 0072).

These records describe provenance, never current authority or permission to run
code. A decoded record must join its original live authority owner before any
qualification, publication or receiving admission. Source-derived qualification
keeps its existing schemas and readers; it is not converted into authored intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, TypeAlias

from ._validation import contract_fields, fail, object_value, string_value
from .identity import ContentIdentity, contract_identity

QUALIFICATION_ORIGIN_SCHEMA = "urn:literate-ai:schema:v2:qualification-origin"
AUTHORED_QUALIFICATION_ORIGIN_SCHEMA = (
    "urn:literate-ai:schema:v2:authored-qualification-origin"
)
SOURCE_DERIVED_QUALIFICATION_ORIGIN_SCHEMA = (
    "urn:literate-ai:schema:v2:source-derived-qualification-origin"
)


def _identity(value: object, path: str) -> None:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")


@dataclass(frozen=True, slots=True)
class AuthoredQualificationOrigin:
    """References to actual authored input and complete locked graph owners.

    Locked generation authority binds the selected root, all authorings, exact
    lock, target and Flavor selection. The input closure additionally binds the
    actual selected document bytes. Neither reference asserts source promotion,
    review authorization, independent acceptance, or successful qualification.
    """

    project_configuration_identity: ContentIdentity
    locked_generation_authority_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    authored_input_closure_identity: ContentIdentity

    SCHEMA: ClassVar[str] = AUTHORED_QUALIFICATION_ORIGIN_SCHEMA
    KIND: ClassVar[str] = "authored"
    _FIELDS: ClassVar[tuple[str, ...]] = (
        "project_configuration_identity",
        "locked_generation_authority_identity",
        "component_lock_identity",
        "authored_input_closure_identity",
    )

    def __post_init__(self) -> None:
        for name in self._FIELDS:
            _identity(getattr(self, name), f"AuthoredQualificationOrigin.{name}")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "origin": self.KIND,
            **{name: getattr(self, name).to_dict() for name in self._FIELDS},
        }

    @classmethod
    def from_dict(cls, value: object) -> AuthoredQualificationOrigin:
        path = "AuthoredQualificationOrigin"
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"origin", *cls._FIELDS}),
        )
        if data["origin"] != cls.KIND:
            fail(f"{path}.origin", "must be authored")
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in cls._FIELDS
            }
        )


@dataclass(frozen=True, slots=True)
class SourceDerivedQualificationOrigin:
    """References to genuine source-derived authority and promotion evidence.

    This new reference record does not upgrade, replace, or authorize reading a
    legacy source-derived result. That result still requires its exact existing
    promotion, inverse-custody, authority-state and recertification validation.
    """

    authority_projection_identity: ContentIdentity
    source_snapshot_identity: ContentIdentity
    generation_input_audit_identity: ContentIdentity
    promotion_tree_identity: ContentIdentity
    promotion_audit_identity: ContentIdentity

    SCHEMA: ClassVar[str] = SOURCE_DERIVED_QUALIFICATION_ORIGIN_SCHEMA
    KIND: ClassVar[str] = "source-derived"
    _FIELDS: ClassVar[tuple[str, ...]] = (
        "authority_projection_identity",
        "source_snapshot_identity",
        "generation_input_audit_identity",
        "promotion_tree_identity",
        "promotion_audit_identity",
    )

    def __post_init__(self) -> None:
        for name in self._FIELDS:
            _identity(getattr(self, name), f"SourceDerivedQualificationOrigin.{name}")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "origin": self.KIND,
            **{name: getattr(self, name).to_dict() for name in self._FIELDS},
        }

    @classmethod
    def from_dict(cls, value: object) -> SourceDerivedQualificationOrigin:
        path = "SourceDerivedQualificationOrigin"
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"origin", *cls._FIELDS}),
        )
        if data["origin"] != cls.KIND:
            fail(f"{path}.origin", "must be source-derived")
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in cls._FIELDS
            }
        )


QualificationOrigin: TypeAlias = (
    AuthoredQualificationOrigin | SourceDerivedQualificationOrigin
)


def qualification_origin_from_dict(value: object) -> QualificationOrigin:
    """Decode exactly one known origin; no fallback or authority restoration."""

    data = object_value(value, "QualificationOrigin")
    schema = string_value(data.get("schema"), "QualificationOrigin.schema")
    if schema == AUTHORED_QUALIFICATION_ORIGIN_SCHEMA:
        return AuthoredQualificationOrigin.from_dict(data)
    if schema == SOURCE_DERIVED_QUALIFICATION_ORIGIN_SCHEMA:
        return SourceDerivedQualificationOrigin.from_dict(data)
    fail("QualificationOrigin.schema", "unknown qualification origin schema")
