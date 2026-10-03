"""Read-only three-way planning contracts for initialized-project updates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, ClassVar

from .identity import ContentIdentity, canonical_identity, contract_identity
from .project_initialization import ProjectInitializationOrigin

PROJECT_UPDATE_FILE_SCHEMA = "urn:literate-ai:schema:v2:project-update-file"
PROJECT_UPDATE_PLAN_SCHEMA = "urn:literate-ai:schema:v2:project-update-plan"
PROJECT_UPDATE_LIMITATIONS = (
    "planning does not apply filesystem changes",
    "dynamic initialized files are preserved and require explicit review",
    "upstream removals outside declared catalog taxonomy cannot be inferred from an "
    "identity-only baseline",
)


class ProjectUpdateClassification(StrEnum):
    UNCHANGED = "unchanged"
    ALREADY_CURRENT = "already-current"
    UPSTREAM_ONLY = "upstream-only"
    LOCAL_ONLY = "local-only"
    CONFLICT = "conflict"
    MERGEABLE = "mergeable"
    UPSTREAM_ADDED = "upstream-added"
    PRESERVED_DYNAMIC = "preserved-dynamic"


@dataclass(frozen=True, slots=True)
class ProjectUpdateFile:
    path: str
    classification: ProjectUpdateClassification
    baseline_identity: ContentIdentity | None
    local_identity: ContentIdentity | None
    upstream_identity: ContentIdentity | None
    base_text: str | None = None
    merged_text: str | None = None

    SCHEMA: ClassVar[str] = PROJECT_UPDATE_FILE_SCHEMA

    def __post_init__(self) -> None:
        parsed = PurePosixPath(self.path)
        if (
            not self.path
            or parsed.is_absolute()
            or parsed.as_posix() != self.path
            or ".." in parsed.parts
            or "\\" in self.path
        ):
            raise ValueError("project update path must be normalized and relative")
        if not isinstance(self.classification, ProjectUpdateClassification):
            raise TypeError("project update classification must be typed")
        for identity in (
            self.baseline_identity,
            self.local_identity,
            self.upstream_identity,
        ):
            if identity is not None and not isinstance(identity, ContentIdentity):
                raise TypeError("project update file identities must be typed")

        if self.base_text is not None:
            import hashlib

            if not isinstance(self.base_text, str) or self.baseline_identity is None:
                raise ValueError("merge base must bind a baseline identity")
            if (
                hashlib.sha256(self.base_text.encode()).hexdigest()
                != self.baseline_identity.digest
            ):
                raise ValueError("merge base identity mismatch")
        if self.classification is ProjectUpdateClassification.MERGEABLE:
            if self.base_text is None or not isinstance(self.merged_text, str):
                raise ValueError("mergeable file requires base and result text")
        elif self.merged_text is not None:
            raise ValueError("only mergeable files may carry merged text")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            **({"base_text": self.base_text} if self.base_text is not None else {}),
            **(
                {"merged_text": self.merged_text}
                if self.merged_text is not None
                else {}
            ),
            "schema": self.SCHEMA,
            "path": self.path,
            "classification": self.classification.value,
            "baseline_identity": (
                None
                if self.baseline_identity is None
                else self.baseline_identity.to_dict()
            ),
            "local_identity": (
                None if self.local_identity is None else self.local_identity.to_dict()
            ),
            "upstream_identity": (
                None
                if self.upstream_identity is None
                else self.upstream_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: object) -> ProjectUpdateFile:
        fields = {
            "schema",
            "path",
            "classification",
            "baseline_identity",
            "local_identity",
            "upstream_identity",
        }
        if (
            not isinstance(value, dict)
            or not fields <= set(value)
            or set(value) - fields - {"base_text", "merged_text"}
        ):
            raise ValueError("project update file must have the exact field set")
        if value["schema"] != cls.SCHEMA:
            raise ValueError("project update file schema is unsupported")

        def optional_identity(candidate: object) -> ContentIdentity | None:
            return None if candidate is None else ContentIdentity.from_dict(candidate)

        return cls(
            value["path"],
            ProjectUpdateClassification(value["classification"]),
            optional_identity(value["baseline_identity"]),
            optional_identity(value["local_identity"]),
            optional_identity(value["upstream_identity"]),
            value.get("base_text"),
            value.get("merged_text"),
        )


@dataclass(frozen=True, slots=True)
class ProjectUpdatePlan:
    project_identity: ContentIdentity
    baseline_identity: ContentIdentity
    previous_origin: ProjectInitializationOrigin
    upstream_origin: ProjectInitializationOrigin
    files: tuple[ProjectUpdateFile, ...]
    update_bases_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = PROJECT_UPDATE_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.project_identity, ContentIdentity):
            raise TypeError("project update project identity must be typed")
        if not isinstance(self.baseline_identity, ContentIdentity):
            raise TypeError("project update baseline identity must be typed")
        if not isinstance(
            self.previous_origin, ProjectInitializationOrigin
        ) or not isinstance(self.upstream_origin, ProjectInitializationOrigin):
            raise TypeError("project update origins must be typed")
        if not isinstance(self.files, tuple) or any(
            not isinstance(item, ProjectUpdateFile) for item in self.files
        ):
            raise TypeError("project update files must be typed")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("project update files must be uniquely sorted")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self._identity_document())

    @property
    def conflicts(self) -> tuple[ProjectUpdateFile, ...]:
        return tuple(
            item
            for item in self.files
            if item.classification is ProjectUpdateClassification.CONFLICT
        )

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
            "limitations": list(PROJECT_UPDATE_LIMITATIONS),
        }

    def _identity_document(self) -> dict[str, Any]:
        return {
            **(
                {"update_bases_identity": self.update_bases_identity.to_dict()}
                if self.update_bases_identity is not None
                else {}
            ),
            "schema": self.SCHEMA,
            "mode": "read-only-plan",
            "apply_supported": False,
            "project_identity": self.project_identity.to_dict(),
            "baseline_identity": self.baseline_identity.to_dict(),
            "previous_origin": self.previous_origin.to_dict(),
            "upstream_origin": self.upstream_origin.to_dict(),
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(cls, value: object) -> ProjectUpdatePlan:
        fields = {
            "schema",
            "identity",
            "mode",
            "apply_supported",
            "project_identity",
            "baseline_identity",
            "previous_origin",
            "upstream_origin",
            "files",
            "counts",
            "limitations",
        }
        if (
            not isinstance(value, dict)
            or not fields <= set(value)
            or set(value) - fields - {"update_bases_identity"}
        ):
            raise ValueError("project update plan must have the exact field set")
        if (
            value["schema"] != cls.SCHEMA
            or value["mode"] != "read-only-plan"
            or value["apply_supported"] is not False
            or not isinstance(value["files"], list)
        ):
            raise ValueError("project update plan protocol is unsupported")
        plan = cls(
            ContentIdentity.from_dict(value["project_identity"]),
            ContentIdentity.from_dict(value["baseline_identity"]),
            ProjectInitializationOrigin.from_dict(value["previous_origin"]),
            ProjectInitializationOrigin.from_dict(value["upstream_origin"]),
            tuple(ProjectUpdateFile.from_dict(item) for item in value["files"]),
            ContentIdentity.from_dict(value["update_bases_identity"])
            if "update_bases_identity" in value
            else None,
        )
        expected = plan.to_dict()
        for derived in ("identity", "counts", "limitations"):
            if value[derived] != expected[derived]:
                raise ValueError(
                    f"project update plan {derived} does not match its content"
                )
        return plan


__all__ = [
    "PROJECT_UPDATE_FILE_SCHEMA",
    "PROJECT_UPDATE_PLAN_SCHEMA",
    "PROJECT_UPDATE_LIMITATIONS",
    "ProjectUpdateClassification",
    "ProjectUpdateFile",
    "ProjectUpdatePlan",
]
