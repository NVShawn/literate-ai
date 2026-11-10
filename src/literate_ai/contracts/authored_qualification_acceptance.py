"""Origin-specific references to actual all-node independent observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail, int_value, list_value
from .identity import ContentIdentity, contract_identity
from .qualification_origins import AuthoredQualificationOrigin

AUTHORED_NODE_ACCEPTANCE_SCHEMA = (
    "urn:literate-ai:schema:v2:authored-node-independent-acceptance"
)
AUTHORED_GRAPH_ACCEPTANCE_SCHEMA = (
    "urn:literate-ai:schema:v2:authored-graph-independent-acceptance"
)


def _identity(value, path):
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")


@dataclass(frozen=True, slots=True)
class AuthoredNodeIndependentAcceptance:
    component_revision_identity: ContentIdentity
    oracle_identity: ContentIdentity
    case_map_identity: ContentIdentity
    observation_identity: ContentIdentity
    case_count: int

    SCHEMA: ClassVar[str] = AUTHORED_NODE_ACCEPTANCE_SCHEMA
    _IDENTITIES: ClassVar[tuple[str, ...]] = (
        "component_revision_identity",
        "oracle_identity",
        "case_map_identity",
        "observation_identity",
    )

    def __post_init__(self):
        for name in self._IDENTITIES:
            _identity(getattr(self, name), name)
        int_value(self.case_count, "case_count", minimum=1)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            **{name: getattr(self, name).to_dict() for name in self._IDENTITIES},
            "case_count": self.case_count,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path="AuthoredNodeIndependentAcceptance",
            schema_uri=cls.SCHEMA,
            required=frozenset({*cls._IDENTITIES, "case_count"}),
        )
        return cls(
            **{name: ContentIdentity.from_dict(data[name]) for name in cls._IDENTITIES},
            case_count=data["case_count"],
        )


@dataclass(frozen=True, slots=True)
class AuthoredGraphIndependentAcceptance:
    """A passive evidence record; receiving still reopens every observation."""

    origin: AuthoredQualificationOrigin
    oracle_set_identity: ContentIdentity
    execution_plan_identity: ContentIdentity
    package_plan_identity: ContentIdentity
    package_result_identity: ContentIdentity
    root_integration_test_identity: ContentIdentity
    packaged_execution_identity: ContentIdentity
    nodes: tuple[AuthoredNodeIndependentAcceptance, ...]

    SCHEMA: ClassVar[str] = AUTHORED_GRAPH_ACCEPTANCE_SCHEMA
    _IDENTITIES: ClassVar[tuple[str, ...]] = (
        "oracle_set_identity",
        "execution_plan_identity",
        "package_plan_identity",
        "package_result_identity",
        "root_integration_test_identity",
        "packaged_execution_identity",
    )

    def __post_init__(self):
        if not isinstance(self.origin, AuthoredQualificationOrigin):
            fail("origin", "must be authored qualification origin")
        for name in self._IDENTITIES:
            _identity(getattr(self, name), name)
        if (
            not isinstance(self.nodes, tuple)
            or not self.nodes
            or any(
                not isinstance(item, AuthoredNodeIndependentAcceptance)
                for item in self.nodes
            )
        ):
            fail("nodes", "must be a nonempty typed tuple")
        revisions = tuple(item.component_revision_identity.uri for item in self.nodes)
        if revisions != tuple(sorted(set(revisions))):
            fail("nodes", "must represent each Component once in canonical order")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "origin": self.origin.to_dict(),
            **{name: getattr(self, name).to_dict() for name in self._IDENTITIES},
            "nodes": [item.to_dict() for item in self.nodes],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path="AuthoredGraphIndependentAcceptance",
            schema_uri=cls.SCHEMA,
            required=frozenset({*cls._IDENTITIES, "origin", "nodes"}),
        )
        return cls(
            origin=AuthoredQualificationOrigin.from_dict(data["origin"]),
            **{name: ContentIdentity.from_dict(data[name]) for name in cls._IDENTITIES},
            nodes=tuple(
                AuthoredNodeIndependentAcceptance.from_dict(item)
                for item in list_value(data["nodes"], "nodes")
            ),
        )
