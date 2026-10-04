from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_project_agent_development_workflow``."""


from literate_ai.contracts import (
    ProjectDefinition,
    ProjectSourceIntelligencePolicy,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)


def _source_intelligence_policy() -> ProjectSourceIntelligencePolicy:
    return ProjectSourceIntelligencePolicy(
        provider_id="none",
        command=None,
        minimum_version=None,
        artifact_path=None,
        stages=tuple(
            (stage, SourceIntelligenceMode.OFF) for stage in SourceIntelligenceStage
        ),
        artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
    )


def _definition(**overrides: object) -> ProjectDefinition:
    fields: dict[str, object] = dict(
        project_id="workflow-project",
        version="1.0.0",
        profile="canonical",
        agent_skill="SKILL.md",
        component_roots=("components",),
        flavor_roots=("flavors",),
        skill_roots=("skills",),
        workflow_roots=("workflows",),
        routing_roots=(),
        documentation_roots=("docs",),
        source_intelligence=_source_intelligence_policy(),
    )
    fields.update(overrides)
    return ProjectDefinition(**fields)
