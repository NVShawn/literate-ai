"""Typed plans for changing a project's repository-parent authority."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .identity import ContentIdentity, canonical_identity
from .repository_lineage import (
    RepositoryLineage,
    RepositoryLineageNode,
    RepositoryParentSelection,
)

REPOSITORY_REPARENT_CHANGE_SCHEMA = (
    "urn:literate-ai:schema:v1:repository-reparent-change"
)
REPOSITORY_REPARENT_PLAN_SCHEMA = "urn:literate-ai:schema:v1:repository-reparent-plan"


class RepositoryReparentDisposition(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class RepositoryReparentChange:
    repository_url: str
    disposition: RepositoryReparentDisposition
    previous_node: RepositoryLineageNode | None
    prospective_node: RepositoryLineageNode | None

    SCHEMA: ClassVar[str] = REPOSITORY_REPARENT_CHANGE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.repository_url, str) or not self.repository_url:
            raise ValueError("repository reparent change URL must be nonempty")
        if not isinstance(self.disposition, RepositoryReparentDisposition):
            raise TypeError("repository reparent disposition must be typed")
        if self.previous_node is not None and not isinstance(
            self.previous_node, RepositoryLineageNode
        ):
            raise TypeError("previous repository-lineage node must be typed")
        if self.prospective_node is not None and not isinstance(
            self.prospective_node, RepositoryLineageNode
        ):
            raise TypeError("prospective repository-lineage node must be typed")
        expected = (
            RepositoryReparentDisposition.ADDED
            if self.previous_node is None
            else RepositoryReparentDisposition.REMOVED
            if self.prospective_node is None
            else RepositoryReparentDisposition.UNCHANGED
            if self.previous_node == self.prospective_node
            else RepositoryReparentDisposition.UPDATED
        )
        if self.disposition is not expected:
            raise ValueError("repository reparent disposition does not match its nodes")
        for node in (self.previous_node, self.prospective_node):
            if node is not None and node.repository_url != self.repository_url:
                raise ValueError(
                    "repository reparent change URL does not match its node"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "repository_url": self.repository_url,
            "disposition": self.disposition.value,
            "previous_node": (
                None if self.previous_node is None else self.previous_node.to_dict()
            ),
            "prospective_node": (
                None
                if self.prospective_node is None
                else self.prospective_node.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class RepositoryReparentPlan:
    project_identity: ContentIdentity
    previous_selection: RepositoryParentSelection
    previous_lineage: RepositoryLineage
    previous_evidence_present: bool
    prospective_selection: RepositoryParentSelection
    prospective_lineage: RepositoryLineage
    changes: tuple[RepositoryReparentChange, ...]

    SCHEMA: ClassVar[str] = REPOSITORY_REPARENT_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.project_identity, ContentIdentity):
            raise TypeError("repository reparent project identity must be typed")
        if not isinstance(
            self.previous_selection, RepositoryParentSelection
        ) or not isinstance(self.prospective_selection, RepositoryParentSelection):
            raise TypeError("repository reparent selections must be typed")
        if not isinstance(self.previous_lineage, RepositoryLineage) or not isinstance(
            self.prospective_lineage, RepositoryLineage
        ):
            raise TypeError("repository reparent lineages must be typed")
        if self.previous_lineage.selection != self.previous_selection:
            raise ValueError("previous lineage does not bind previous selection")
        if not isinstance(self.previous_evidence_present, bool):
            raise TypeError(
                "previous repository-lineage evidence state must be boolean"
            )
        if not self.previous_evidence_present and (
            self.previous_selection != RepositoryParentSelection.root()
            or self.previous_lineage.nodes
            or self.previous_lineage.selected_parents
        ):
            raise ValueError(
                "absent previous evidence must be represented as a typed root lineage"
            )
        if self.prospective_lineage.selection != self.prospective_selection:
            raise ValueError("prospective lineage does not bind prospective selection")
        urls = tuple(item.repository_url for item in self.changes)
        if urls != tuple(sorted(set(urls))):
            raise ValueError("repository reparent changes must be uniquely sorted")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self._identity_document())

    @property
    def changed(self) -> bool:
        return (
            not self.previous_evidence_present
            or self.previous_lineage != self.prospective_lineage
        )

    def to_dict(self) -> dict[str, Any]:
        counts = {
            disposition.value: sum(
                item.disposition is disposition for item in self.changes
            )
            for disposition in RepositoryReparentDisposition
        }
        return {
            **self._identity_document(),
            "identity": self.identity.to_dict(),
            "counts": counts,
            "changed": self.changed,
            "apply_supported": True,
            "authority_review_required": self.changed,
        }

    def _identity_document(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "mode": "read-only-plan",
            "project_identity": self.project_identity.to_dict(),
            "previous_selection": self.previous_selection.to_dict(),
            "previous_lineage": self.previous_lineage.to_dict(),
            "previous_evidence_present": self.previous_evidence_present,
            "prospective_selection": self.prospective_selection.to_dict(),
            "prospective_lineage": self.prospective_lineage.to_dict(),
            "changes": [item.to_dict() for item in self.changes],
        }


__all__ = [
    "REPOSITORY_REPARENT_CHANGE_SCHEMA",
    "REPOSITORY_REPARENT_PLAN_SCHEMA",
    "RepositoryReparentChange",
    "RepositoryReparentDisposition",
    "RepositoryReparentPlan",
]
