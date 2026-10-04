from __future__ import annotations

import unittest

from literate_ai.contracts import (
    ContractValidationError,
    ProjectDefinition,
    ProjectSourceIntelligencePolicy,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


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


class ProjectDefinitionAgentDevelopmentWorkflowTests(unittest.TestCase):
    def test_default_is_dev_and_is_omitted_from_the_wire_format(self) -> None:
        definition = _definition()

        self.assertEqual(definition.agent_development_workflow, "dev")
        self.assertNotIn("agent_development_workflow", definition.to_dict())
        self.assertEqual(ProjectDefinition.from_dict(definition.to_dict()), definition)

    def test_explicit_non_default_value_round_trips_and_validates(self) -> None:
        definition = _definition(agent_development_workflow="staging")

        wire = definition.to_dict()
        self.assertEqual(wire["agent_development_workflow"], "staging")
        self.assertEqual(ProjectDefinition.from_dict(wire), definition)
        SchemaCatalog().validate(definition.SCHEMA, wire)

    def test_production_value_round_trips(self) -> None:
        definition = _definition(agent_development_workflow="production")

        self.assertEqual(ProjectDefinition.from_dict(definition.to_dict()), definition)

    def test_invalid_value_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError):
            _definition(agent_development_workflow="prod")


if __name__ == "__main__":
    unittest.main()
