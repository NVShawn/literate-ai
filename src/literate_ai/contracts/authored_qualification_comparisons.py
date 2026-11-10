"""Authored behavioral comparison references without a source-parity baseline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail, list_value
from .authored_qualification_acceptance import AuthoredNodeIndependentAcceptance
from .identity import ContentIdentity, contract_identity
from .qualification_origins import AuthoredQualificationOrigin


@dataclass(frozen=True, slots=True)
class AuthoredQualificationComparison:
    """Passive references to reopened native observations and sealed case maps."""

    origin: AuthoredQualificationOrigin
    plan_identity: ContentIdentity
    run_identity: ContentIdentity
    oracle_set_identity: ContentIdentity
    independent_acceptance_identity: ContentIdentity
    nodes: tuple[AuthoredNodeIndependentAcceptance, ...]

    SCHEMA: ClassVar[str] = (
        "urn:literate-ai:schema:v2:authored-qualification-comparison"
    )
    _IDENTITIES: ClassVar[tuple[str, ...]] = (
        "plan_identity",
        "run_identity",
        "oracle_set_identity",
        "independent_acceptance_identity",
    )

    def __post_init__(self):
        if not isinstance(self.origin, AuthoredQualificationOrigin):
            fail("origin", "must be an authored qualification origin")
        for name in self._IDENTITIES:
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(name, "must be a ContentIdentity")
        if (
            not isinstance(self.nodes, tuple)
            or not self.nodes
            or any(
                not isinstance(n, AuthoredNodeIndependentAcceptance) for n in self.nodes
            )
        ):
            fail("nodes", "must be a nonempty typed tuple")
        keys = tuple(n.component_revision_identity.uri for n in self.nodes)
        if keys != tuple(sorted(set(keys))):
            fail("nodes", "must name every selected Component once in canonical order")

    @property
    def case_count(self):
        return sum(n.case_count for n in self.nodes)

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "origin": self.origin.to_dict(),
            **{n: getattr(self, n).to_dict() for n in self._IDENTITIES},
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"origin", "nodes", *cls._IDENTITIES}),
        )
        return cls(
            origin=AuthoredQualificationOrigin.from_dict(data["origin"]),
            **{n: ContentIdentity.from_dict(data[n]) for n in cls._IDENTITIES},
            nodes=tuple(
                AuthoredNodeIndependentAcceptance.from_dict(n)
                for n in list_value(data["nodes"], "nodes")
            ),
        )
