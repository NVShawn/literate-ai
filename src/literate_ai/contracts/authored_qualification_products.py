"""Authored run/product references; integrity metadata, never admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail, list_value
from .identity import ContentIdentity, contract_identity
from .qualification_origins import AuthoredQualificationOrigin


@dataclass(frozen=True, slots=True)
class AuthoredQualificationRunProducts:
    run_identity: ContentIdentity
    run_record_identity: ContentIdentity
    export_set_identity: ContentIdentity

    SCHEMA: ClassVar[str] = (
        "urn:literate-ai:schema:v2:authored-qualification-run-products"
    )
    _FIELDS: ClassVar[tuple[str, ...]] = (
        "run_identity",
        "run_record_identity",
        "export_set_identity",
    )

    def __post_init__(self):
        for name in self._FIELDS:
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(name, "must be a ContentIdentity")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            **{n: getattr(self, n).to_dict() for n in self._FIELDS},
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(cls._FIELDS),
        )
        return cls(**{n: ContentIdentity.from_dict(data[n]) for n in cls._FIELDS})


@dataclass(frozen=True, slots=True)
class AuthoredQualificationProducts:
    origin: AuthoredQualificationOrigin
    qualification_identity: ContentIdentity
    runs: tuple[AuthoredQualificationRunProducts, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:authored-qualification-products"

    def __post_init__(self):
        if not isinstance(self.origin, AuthoredQualificationOrigin):
            fail("origin", "must be an authored qualification origin")
        if not isinstance(self.qualification_identity, ContentIdentity):
            fail("qualification_identity", "must be a ContentIdentity")
        if (
            not isinstance(self.runs, tuple)
            or len(self.runs) < 2
            or any(
                not isinstance(r, AuthoredQualificationRunProducts) for r in self.runs
            )
        ):
            fail("runs", "must contain every required authored run")
        keys = tuple(r.run_identity.uri for r in self.runs)
        if keys != tuple(sorted(set(keys))):
            fail("runs", "must name each run once in canonical order")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "origin": self.origin.to_dict(),
            "qualification_identity": self.qualification_identity.to_dict(),
            "runs": [r.to_dict() for r in self.runs],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"origin", "qualification_identity", "runs"}),
        )
        return cls(
            AuthoredQualificationOrigin.from_dict(data["origin"]),
            ContentIdentity.from_dict(data["qualification_identity"]),
            tuple(
                AuthoredQualificationRunProducts.from_dict(r)
                for r in list_value(data["runs"], "runs")
            ),
        )
