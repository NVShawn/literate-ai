"""Exact read-only plans for updating an inherited repository lineage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from .identity import ContentIdentity, canonical_identity
from .project_updates import ProjectUpdateClassification, ProjectUpdateFile
from .repository_lineage import RepositoryLineage

REPOSITORY_LINEAGE_UPDATE_PLAN_SCHEMA = (
    "urn:literate-ai:schema:v1:repository-lineage-update-plan"
)
REPOSITORY_LINEAGE_UPDATE_APPLY_SCHEMA = (
    "urn:literate-ai:schema:v2:repository-lineage-update-apply"
)


@dataclass(frozen=True, slots=True)
class RepositoryLineageUpdatePlan:
    project_identity: ContentIdentity
    previous_lineage: RepositoryLineage
    prospective_lineage: RepositoryLineage
    files: tuple[ProjectUpdateFile, ...]

    SCHEMA: ClassVar[str] = REPOSITORY_LINEAGE_UPDATE_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.project_identity, ContentIdentity):
            raise TypeError("repository update project identity must be typed")
        if not isinstance(self.previous_lineage, RepositoryLineage) or not isinstance(
            self.prospective_lineage, RepositoryLineage
        ):
            raise TypeError("repository update lineages must be typed")
        if self.previous_lineage.selection != self.prospective_lineage.selection:
            raise ValueError(
                "repository update cannot silently change parent selection"
            )
        if not isinstance(self.files, tuple) or any(
            not isinstance(item, ProjectUpdateFile) for item in self.files
        ):
            raise TypeError("repository update files must be typed")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("repository update files must be uniquely sorted")

    @property
    def changed(self) -> bool:
        return self.previous_lineage != self.prospective_lineage or any(
            item.classification
            not in (
                ProjectUpdateClassification.UNCHANGED,
                ProjectUpdateClassification.ALREADY_CURRENT,
                ProjectUpdateClassification.LOCAL_ONLY,
                ProjectUpdateClassification.PRESERVED_DYNAMIC,
            )
            for item in self.files
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self._identity_document())

    def _identity_document(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "mode": "read-only-plan",
            "project_identity": self.project_identity.to_dict(),
            "previous_lineage": self.previous_lineage.to_dict(),
            "prospective_lineage": self.prospective_lineage.to_dict(),
            "files": [item.to_dict() for item in self.files],
        }

    def to_dict(self) -> dict[str, Any]:
        counts = {
            classification.value: sum(
                item.classification is classification for item in self.files
            )
            for classification in ProjectUpdateClassification
        }
        return {
            **self._identity_document(),
            "identity": self.identity.to_dict(),
            "counts": counts,
            "changed": self.changed,
            "apply_supported": True,
            "limitations": [],
        }

    @classmethod
    def from_dict(cls, value: object) -> RepositoryLineageUpdatePlan:
        fields = {
            "schema",
            "mode",
            "project_identity",
            "previous_lineage",
            "prospective_lineage",
            "files",
            "identity",
            "counts",
            "changed",
            "apply_supported",
            "limitations",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("repository lineage update plan has invalid fields")
        if (
            value["schema"] != cls.SCHEMA
            or value["mode"] != "read-only-plan"
            or value["apply_supported"] is not True
            or not isinstance(value["files"], list)
        ):
            raise ValueError("repository lineage update plan protocol is unsupported")
        plan = cls(
            ContentIdentity.from_dict(value["project_identity"]),
            RepositoryLineage.from_dict(value["previous_lineage"]),
            RepositoryLineage.from_dict(value["prospective_lineage"]),
            tuple(ProjectUpdateFile.from_dict(item) for item in value["files"]),
        )
        expected = plan.to_dict()
        for derived in ("identity", "counts", "changed", "limitations"):
            if value[derived] != expected[derived]:
                raise ValueError(
                    f"repository lineage update {derived} does not match its content"
                )
        return plan


@dataclass(frozen=True, slots=True)
class AppliedRepositoryLineageUpdate:
    plan_identity: ContentIdentity
    previous_lineage_identity: ContentIdentity
    prospective_lineage_identity: ContentIdentity
    provenance_identity: ContentIdentity
    applied: tuple[str, ...]
    adopted: tuple[str, ...]
    taken_upstream: tuple[str, ...]
    kept_local: tuple[str, ...]
    refused: tuple[tuple[ProjectUpdateClassification, tuple[str, ...]], ...]

    SCHEMA: ClassVar[str] = REPOSITORY_LINEAGE_UPDATE_APPLY_SCHEMA

    def __post_init__(self) -> None:
        for identity in (
            self.plan_identity,
            self.previous_lineage_identity,
            self.prospective_lineage_identity,
            self.provenance_identity,
        ):
            if not isinstance(identity, ContentIdentity):
                raise TypeError("repository update apply identities must be typed")
        for paths in (
            self.applied,
            self.adopted,
            self.taken_upstream,
            self.kept_local,
        ):
            if paths != tuple(sorted(set(paths))):
                raise ValueError(
                    "repository update apply paths must be uniquely sorted"
                )
        keys = tuple(item[0].value for item in self.refused)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("repository update refusals must be uniquely sorted")
        if any(paths != tuple(sorted(set(paths))) for _kind, paths in self.refused):
            raise ValueError("repository update refused paths must be uniquely sorted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "plan_identity": self.plan_identity.to_dict(),
            "previous_lineage_identity": self.previous_lineage_identity.to_dict(),
            "prospective_lineage_identity": self.prospective_lineage_identity.to_dict(),
            "provenance_identity": self.provenance_identity.to_dict(),
            "applied": list(self.applied),
            "adopted": list(self.adopted),
            "taken_upstream": list(self.taken_upstream),
            "kept_local": list(self.kept_local),
            "refused": {
                classification.value: list(paths)
                for classification, paths in self.refused
            },
            "authority_review_required": (
                self.previous_lineage_identity != self.prospective_lineage_identity
                or bool(
                    self.applied
                    or self.adopted
                    or self.taken_upstream
                    or self.kept_local
                )
            ),
        }


__all__ = [
    "REPOSITORY_LINEAGE_UPDATE_APPLY_SCHEMA",
    "REPOSITORY_LINEAGE_UPDATE_PLAN_SCHEMA",
    "AppliedRepositoryLineageUpdate",
    "RepositoryLineageUpdatePlan",
]
