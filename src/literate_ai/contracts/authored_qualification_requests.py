"""Truthful authored lifecycle invocation references; never execution grants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail
from .identity import ContentIdentity, contract_identity
from .qualification_origins import AuthoredQualificationOrigin


@dataclass(frozen=True, slots=True)
class AuthoredQualificationLifecycleRequest:
    """One clean run selected by an acknowledged authored plan.

    A decoded request cannot recover its current project/oracle owners, an
    allocated workspace, reviewed generation authority or host permission.
    Those owners remain obligations of the executing composition.
    """

    origin: AuthoredQualificationOrigin
    plan_identity: ContentIdentity
    oracle_set_identity: ContentIdentity
    run_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    force_regeneration: bool = True

    SCHEMA: ClassVar[str] = (
        "urn:literate-ai:schema:v2:authored-qualification-lifecycle-request"
    )
    _IDENTITIES: ClassVar[tuple[str, ...]] = (
        "plan_identity",
        "oracle_set_identity",
        "run_identity",
        "workspace_allocation_identity",
    )

    def __post_init__(self):
        if not isinstance(self.origin, AuthoredQualificationOrigin):
            fail("origin", "must be an authored qualification origin")
        for name in self._IDENTITIES:
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(name, "must be a ContentIdentity")
        if self.force_regeneration is not True:
            fail("force_regeneration", "must force a clean all-node generation")

    @property
    def component_lock_identity(self):
        return self.origin.component_lock_identity

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "origin": self.origin.to_dict(),
            **{name: getattr(self, name).to_dict() for name in self._IDENTITIES},
            "force_regeneration": self.force_regeneration,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path="AuthoredQualificationLifecycleRequest",
            schema_uri=cls.SCHEMA,
            required=frozenset({"origin", "force_regeneration", *cls._IDENTITIES}),
        )
        return cls(
            origin=AuthoredQualificationOrigin.from_dict(data["origin"]),
            **{name: ContentIdentity.from_dict(data[name]) for name in cls._IDENTITIES},
            force_regeneration=data["force_regeneration"],
        )
