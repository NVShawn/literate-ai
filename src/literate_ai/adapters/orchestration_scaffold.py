"""Deterministic canonical root additions, not an initialization transaction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable

from literate_ai.application.project_authority import (
    AUTHORITY_REVIEW_PLACEHOLDER,
    AuthorityReviewDocument,
    ProjectAuthorityInventory,
    review_project_authority,
)
from literate_ai.contracts import RepositoryLineage, RepositoryParentSelection
from literate_ai.contracts.identity import (
    ContentIdentity,
    HashAlgorithm,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.projects import (
    CANONICAL_PROJECT_PROFILE,
    ProjectDefinition,
    ProjectSourceIntelligencePolicy,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)
from literate_ai.contracts.repository_orchestration import RepositoryOrchestration
from literate_ai.projects import serialize_project_configuration

from .repository_lineage import REPOSITORY_LINEAGE_FILE, REPOSITORY_PARENT_FILE

_NAMESPACE = ".literate/orchestration"
_GUIDE = f"{_NAMESPACE}/docs/overview.md"
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


def _identity(content: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())


@dataclass(frozen=True, slots=True)
class OrchestrationScaffold:
    """Owned bytes; callers must still review, revalidate and transact publication."""

    definition: ProjectDefinition
    files: tuple[tuple[str, bytes], ...]

    @property
    def identity(self) -> str:
        return canonical_identity(
            {path: _identity(content).uri for path, content in self.files}
        ).uri


def prepare_orchestration_scaffold(
    authority: RepositoryOrchestration,
    *,
    project_id: str,
    version: str,
    template_root: Traversable | None = None,
) -> OrchestrationScaffold:
    """Prepare exact bytes from installed assets or explicit admission templates."""
    if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError(
            "orchestration project ID requires a portable 1..64 character ID"
        )
    if not isinstance(authority, RepositoryOrchestration):
        raise TypeError("orchestration scaffold requires typed repository authority")
    definition = ProjectDefinition(
        project_id=project_id,
        version=version,
        profile=CANONICAL_PROJECT_PROFILE,
        agent_skill="SKILL.md",
        component_roots=(),
        flavor_roots=(),
        skill_roots=(f"{_NAMESPACE}/skills",),
        workflow_roots=(),
        routing_roots=(),
        documentation_roots=(f"{_NAMESPACE}/docs",),
        source_intelligence=ProjectSourceIntelligencePolicy(
            "none",
            None,
            None,
            None,
            tuple(
                (stage, SourceIntelligenceMode.OFF) for stage in SourceIntelligenceStage
            ),
            SourceIntelligenceArtifactPublication.METADATA_ONLY,
        ),
        log_dir=f"{_NAMESPACE}/logs",
        repository_orchestration=authority,
    )
    templates = (
        files("literate_ai.project_template").joinpath("orchestration")
        if template_root is None
        else template_root
    )
    content = {
        destination: templates.joinpath(resource)
        .read_text(encoding="utf-8")
        .encode("utf-8")
        for destination, resource in (
            ("SKILL.md", "onboarding.md"),
            ("PROJECT.md", "project.md"),
            (_GUIDE, "overview.md"),
            (f"{_NAMESPACE}/skills/agent/SKILL.md", "agent.md"),
        )
    }
    selection = RepositoryParentSelection.root()
    lineage = RepositoryLineage(selection, (), ())
    review = review_project_authority(
        ProjectAuthorityInventory(
            definition.identity,
            lineage.identity,
            _identity(content["SKILL.md"]),
            documentation=(AuthorityReviewDocument(_GUIDE, content[_GUIDE]),),
        )
    )
    placeholder = AUTHORITY_REVIEW_PLACEHOLDER.encode("utf-8")
    if content[_GUIDE].count(placeholder) != 1:
        raise ValueError("orchestration guide requires exactly one review placeholder")
    content[_GUIDE] = content[_GUIDE].replace(
        placeholder, review.expected_marker.encode("utf-8")
    )
    content.update(
        {
            "literate.project.json": serialize_project_configuration(definition),
            REPOSITORY_PARENT_FILE: canonical_json_bytes(selection.to_dict()) + b"\n",
            REPOSITORY_LINEAGE_FILE: canonical_json_bytes(lineage.to_dict()) + b"\n",
        }
    )
    return OrchestrationScaffold(definition, tuple(sorted(content.items())))
