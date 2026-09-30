"""Pure project documentation-authority review calculation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from literate_ai.contracts import ContentIdentity, canonical_identity

AUTHORITY_REVIEW_INPUT_SCHEMA = "literate-ai/authority-review-input@2"
AUTHORITY_REVIEW_SCHEMA = "literate-ai/authority-review@1"
AUTHORITY_REVIEW_PLACEHOLDER = "<!-- literate-ai:authority-review-pending -->"
AUTHORITY_REVIEW_MARKER = re.compile(
    rb"<!--\s*literate-ai:authority-reviewed sha256:([0-9a-f]{64})\s*-->"
)
_EXECUTION_QUEUE_ROOT = "docs"
_EXECUTION_QUEUE_DIRECTORY = "roadmap"


def documentation_path_is_execution_queue(path: str) -> bool:
    """Return whether a project-relative document is the tactical execution queue.

    Queue files remain in the documentation graph so links and assets still fail
    closed. They are omitted from the authority-review hash so checkbox churn is
    not documentation-authority drift.
    """

    parts = tuple(part for part in path.replace("\\", "/").split("/") if part)
    try:
        index = parts.index(_EXECUTION_QUEUE_ROOT)
    except ValueError:
        return False
    return index + 2 < len(parts) and parts[index + 1] == _EXECUTION_QUEUE_DIRECTORY


@dataclass(frozen=True, slots=True)
class ComponentAuthorityReviewEntry:
    coordinate: str
    revision: str
    workflow: str
    routing: str
    input_closure: str
    authority_convergence: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "coordinate": self.coordinate,
            "revision": self.revision,
            "workflow": self.workflow,
            "routing": self.routing,
            "input_closure": self.input_closure,
            "authority_convergence": self.authority_convergence,
        }


@dataclass(frozen=True, slots=True)
class FlavorAuthorityReviewEntry:
    coordinate: str
    revision: str
    specifications: str

    def to_dict(self) -> dict[str, str]:
        return {
            "coordinate": self.coordinate,
            "revision": self.revision,
            "specifications": self.specifications,
        }


@dataclass(frozen=True, slots=True)
class ForwardSkillAuthorityReviewEntry:
    skill_id: str
    version: str
    identity: str

    def to_dict(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "identity": self.identity,
        }


@dataclass(frozen=True, slots=True)
class InverseSkillAuthorityReviewEntry:
    skill_id: str
    version: str
    content_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class CatalogAuthorityReviewEntry:
    path: str
    identity: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "identity": self.identity}


@dataclass(frozen=True, slots=True)
class AuthorityReviewDocument:
    path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ProjectAuthorityInventory:
    """All inputs that make one project documentation review current."""

    project_definition: ContentIdentity
    repository_lineage: ContentIdentity
    onboarding_skill: ContentIdentity
    components: tuple[ComponentAuthorityReviewEntry, ...] = ()
    flavors: tuple[FlavorAuthorityReviewEntry, ...] = ()
    specification_to_source_skills: tuple[ForwardSkillAuthorityReviewEntry, ...] = ()
    source_to_specification_skills: tuple[InverseSkillAuthorityReviewEntry, ...] = ()
    workflows: tuple[CatalogAuthorityReviewEntry, ...] = ()
    routing: tuple[CatalogAuthorityReviewEntry, ...] = ()
    documentation: tuple[AuthorityReviewDocument, ...] = ()
    documentation_assets: tuple[AuthorityReviewDocument, ...] = ()

    def to_canonical_value(self) -> dict[str, object]:
        placeholder = AUTHORITY_REVIEW_PLACEHOLDER.encode("utf-8")
        return {
            "schema": AUTHORITY_REVIEW_INPUT_SCHEMA,
            "project_definition": self.project_definition.uri,
            "repository_lineage": self.repository_lineage.uri,
            "onboarding_skill": self.onboarding_skill.uri,
            "components": [
                item.to_dict()
                for item in sorted(self.components, key=lambda item: item.coordinate)
            ],
            # These sequences retain caller order for byte compatibility with the
            # original project validator. Their adapters provide canonical order.
            "flavors": [item.to_dict() for item in self.flavors],
            "specification_to_source_skills": [
                item.to_dict() for item in self.specification_to_source_skills
            ],
            "source_to_specification_skills": [
                item.to_dict() for item in self.source_to_specification_skills
            ],
            "workflows": [item.to_dict() for item in self.workflows],
            "routing": [item.to_dict() for item in self.routing],
            "documentation": [
                {
                    "path": item.path,
                    "identity": "sha256:"
                    + hashlib.sha256(
                        AUTHORITY_REVIEW_MARKER.sub(b"", item.content).replace(
                            placeholder, b""
                        )
                    ).hexdigest(),
                }
                for item in sorted(self.documentation, key=lambda item: item.path)
            ],
            "documentation_assets": [
                {
                    "path": item.path,
                    "identity": "sha256:" + hashlib.sha256(item.content).hexdigest(),
                }
                for item in sorted(
                    self.documentation_assets, key=lambda item: item.path
                )
            ],
        }


class ProjectAuthorityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProjectAuthorityReview:
    state: str
    authority_identity: ContentIdentity
    expected_marker: str
    document: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": AUTHORITY_REVIEW_SCHEMA,
            "state": self.state,
            "authority_identity": self.authority_identity.uri,
            "expected_marker": self.expected_marker,
            "document": self.document,
        }


def review_project_authority(
    inventory: ProjectAuthorityInventory, *, required: bool = False
) -> ProjectAuthorityReview:
    """Review marker state without reading files or depending on a CLI surface."""

    identity = canonical_identity(inventory.to_canonical_value())
    expected = f"<!-- literate-ai:authority-reviewed {identity.uri} -->"
    matches = [
        (document.path, match.group(1).decode("ascii"))
        for document in sorted(inventory.documentation, key=lambda item: item.path)
        for match in AUTHORITY_REVIEW_MARKER.finditer(document.content)
    ]
    state = (
        "missing"
        if not matches
        else "duplicate"
        if len(matches) != 1
        else "current"
        if matches[0][1] == identity.digest
        else "stale"
    )
    if required and state != "current":
        message = (
            "documentation authority review is "
            + state
            + "; review current authority and record exactly: "
            + expected
        )
        raise ProjectAuthorityError(
            "project.documentation_authority_review_" + state, message
        )
    return ProjectAuthorityReview(
        state=state,
        authority_identity=identity,
        expected_marker=expected,
        document=matches[0][0] if len(matches) == 1 else None,
    )


__all__ = [
    "AUTHORITY_REVIEW_INPUT_SCHEMA",
    "AUTHORITY_REVIEW_MARKER",
    "AUTHORITY_REVIEW_PLACEHOLDER",
    "AUTHORITY_REVIEW_SCHEMA",
    "documentation_path_is_execution_queue",
    "AuthorityReviewDocument",
    "CatalogAuthorityReviewEntry",
    "ComponentAuthorityReviewEntry",
    "FlavorAuthorityReviewEntry",
    "ForwardSkillAuthorityReviewEntry",
    "InverseSkillAuthorityReviewEntry",
    "ProjectAuthorityError",
    "ProjectAuthorityInventory",
    "ProjectAuthorityReview",
    "review_project_authority",
]
