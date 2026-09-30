"""Exact, identity-bearing Component invalidation decisions and tables."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import contract_fields, enum_value, fail, parse_tuple, unique
from ..identity import ContentIdentity, contract_identity
from ._common import canonical_identities, identity, portable_name, tuple_value

COMPONENT_INVALIDATION_DECISION_SCHEMA = (
    "urn:literate-ai:schema:v2:component-invalidation-decision"
)
COMPONENT_INVALIDATION_TABLE_SCHEMA = (
    "urn:literate-ai:schema:v2:component-invalidation-table"
)


class ComponentChangeSurface(StrEnum):
    LOCAL_AUTHORITY = "local-authority"
    TARGET_OR_FLAVOR = "target-or-flavor"
    PUBLIC_INTERFACE = "public-interface"
    SOURCE_REPLACEMENT = "source-replacement"
    AUTHORED_ASSET = "authored-asset"


@dataclass(frozen=True, slots=True)
class ComponentInvalidationDecision:
    """Exact action membership caused by one classified Component change."""

    case_id: str
    changed_component: ContentIdentity
    surface: ComponentChangeSurface
    regenerate: tuple[ContentIdentity, ...]
    rebuild: tuple[ContentIdentity, ...]
    retest: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = COMPONENT_INVALIDATION_DECISION_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.case_id, "ComponentInvalidationDecision.case_id")
        identity(
            self.changed_component,
            "ComponentInvalidationDecision.changed_component",
        )
        if not isinstance(self.surface, ComponentChangeSurface):
            fail(
                "ComponentInvalidationDecision.surface",
                "must be a ComponentChangeSurface",
            )
        regenerate = canonical_identities(
            self.regenerate, "ComponentInvalidationDecision.regenerate"
        )
        rebuild = canonical_identities(
            self.rebuild, "ComponentInvalidationDecision.rebuild", required=True
        )
        retest = canonical_identities(
            self.retest, "ComponentInvalidationDecision.retest", required=True
        )
        regenerate_set = {item.uri for item in regenerate}
        rebuild_set = {item.uri for item in rebuild}
        retest_set = {item.uri for item in retest}
        if not regenerate_set <= rebuild_set or not rebuild_set <= retest_set:
            fail(
                "ComponentInvalidationDecision",
                "must satisfy regenerate subset rebuild subset retest",
            )
        if self.changed_component.uri not in retest_set:
            fail(
                "ComponentInvalidationDecision.retest",
                "must include the changed Component",
            )
        generation_change = self.surface in {
            ComponentChangeSurface.LOCAL_AUTHORITY,
            ComponentChangeSurface.TARGET_OR_FLAVOR,
            ComponentChangeSurface.PUBLIC_INTERFACE,
        }
        if generation_change != (self.changed_component.uri in regenerate_set):
            fail(
                "ComponentInvalidationDecision.regenerate",
                "changed Component membership contradicts the change surface",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "case_id": self.case_id,
            "changed_component": self.changed_component.to_dict(),
            "surface": self.surface.value,
            "regenerate": [item.to_dict() for item in self.regenerate],
            "rebuild": [item.to_dict() for item in self.rebuild],
            "retest": [item.to_dict() for item in self.retest],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentInvalidationDecision"
    ) -> ComponentInvalidationDecision:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "case_id",
                    "changed_component",
                    "surface",
                    "regenerate",
                    "rebuild",
                    "retest",
                }
            ),
        )
        return cls(
            case_id=portable_name(data["case_id"], f"{path}.case_id"),
            changed_component=ContentIdentity.from_dict(
                data["changed_component"], path=f"{path}.changed_component"
            ),
            surface=enum_value(
                ComponentChangeSurface, data["surface"], f"{path}.surface"
            ),
            regenerate=parse_tuple(
                data["regenerate"], f"{path}.regenerate", ContentIdentity.from_dict
            ),
            rebuild=parse_tuple(
                data["rebuild"], f"{path}.rebuild", ContentIdentity.from_dict
            ),
            retest=parse_tuple(
                data["retest"], f"{path}.retest", ContentIdentity.from_dict
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentInvalidationTable:
    """Canonical invalidation decisions for one exact Component graph."""

    component_graph_identity: ContentIdentity
    component_revisions: tuple[ContentIdentity, ...]
    decisions: tuple[ComponentInvalidationDecision, ...]

    SCHEMA: ClassVar[str] = COMPONENT_INVALIDATION_TABLE_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.component_graph_identity,
            "ComponentInvalidationTable.component_graph_identity",
        )
        revisions = canonical_identities(
            self.component_revisions,
            "ComponentInvalidationTable.component_revisions",
            required=True,
        )
        decisions = tuple_value(self.decisions, "ComponentInvalidationTable.decisions")
        if (
            not decisions
            or len(decisions) > 256
            or any(
                not isinstance(item, ComponentInvalidationDecision)
                for item in decisions
            )
        ):
            fail(
                "ComponentInvalidationTable.decisions",
                "must contain 1 to 256 ComponentInvalidationDecision values",
            )
        case_ids = tuple(item.case_id for item in self.decisions)
        unique(case_ids, "ComponentInvalidationTable.decisions", "case IDs")
        if case_ids != tuple(sorted(case_ids)):
            fail(
                "ComponentInvalidationTable.decisions",
                "must use canonical case order",
            )
        admitted = {item.uri for item in revisions}
        for decision in self.decisions:
            referenced = {
                decision.changed_component.uri,
                *(item.uri for item in decision.regenerate),
                *(item.uri for item in decision.rebuild),
                *(item.uri for item in decision.retest),
            }
            if not referenced <= admitted:
                fail(
                    "ComponentInvalidationTable.decisions",
                    "a decision references a Component outside the exact graph",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_graph_identity": self.component_graph_identity.to_dict(),
            "component_revisions": [
                item.to_dict() for item in self.component_revisions
            ],
            "decisions": [item.to_dict() for item in self.decisions],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentInvalidationTable"
    ) -> ComponentInvalidationTable:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"component_graph_identity", "component_revisions", "decisions"}
            ),
        )
        return cls(
            component_graph_identity=ContentIdentity.from_dict(
                data["component_graph_identity"],
                path=f"{path}.component_graph_identity",
            ),
            component_revisions=parse_tuple(
                data["component_revisions"],
                f"{path}.component_revisions",
                ContentIdentity.from_dict,
            ),
            decisions=parse_tuple(
                data["decisions"],
                f"{path}.decisions",
                ComponentInvalidationDecision.from_dict,
            ),
        )


__all__ = [
    "COMPONENT_INVALIDATION_DECISION_SCHEMA",
    "COMPONENT_INVALIDATION_TABLE_SCHEMA",
    "ComponentChangeSurface",
    "ComponentInvalidationDecision",
    "ComponentInvalidationTable",
]
