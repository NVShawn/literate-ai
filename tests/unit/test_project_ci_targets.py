from __future__ import annotations

import unittest

from literate_ai.contracts import (
    CiExecutionMode,
    CiTarget,
    CiTargetPreference,
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
        project_id="ci-target-project",
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


class CiTargetPreferenceTests(unittest.TestCase):
    def test_round_trips(self) -> None:
        preference = CiTargetPreference(CiTarget.LOCAL, CiExecutionMode.SERIAL)

        self.assertEqual(CiTargetPreference.from_dict(preference.to_dict()), preference)

    def test_rejects_untyped_target(self) -> None:
        with self.assertRaises(ContractValidationError):
            CiTargetPreference("local", CiExecutionMode.SERIAL)

    def test_rejects_untyped_mode(self) -> None:
        with self.assertRaises(ContractValidationError):
            CiTargetPreference(CiTarget.LOCAL, "serial")


class ProjectDefinitionCiTargetsTests(unittest.TestCase):
    def test_default_is_empty_and_omitted_from_the_wire_format(self) -> None:
        definition = _definition()

        self.assertEqual(definition.ci_targets, ())
        self.assertNotIn("ci_targets", definition.to_dict())
        self.assertEqual(ProjectDefinition.from_dict(definition.to_dict()), definition)

    def test_ordered_local_then_github_round_trips_and_validates(self) -> None:
        definition = _definition(
            ci_targets=(
                CiTargetPreference(CiTarget.LOCAL, CiExecutionMode.PARALLEL),
                CiTargetPreference(CiTarget.GITHUB, CiExecutionMode.SERIAL),
            )
        )

        wire = definition.to_dict()
        self.assertEqual(
            wire["ci_targets"],
            [
                {
                    "schema": "urn:literate-ai:schema:v2:ci-target-preference",
                    "target": "local",
                    "mode": "parallel",
                },
                {
                    "schema": "urn:literate-ai:schema:v2:ci-target-preference",
                    "target": "github",
                    "mode": "serial",
                },
            ],
        )
        self.assertEqual(ProjectDefinition.from_dict(wire), definition)
        SchemaCatalog().validate(definition.SCHEMA, wire)

    def test_local_and_gitlab_may_combine(self) -> None:
        definition = _definition(
            ci_targets=(
                CiTargetPreference(CiTarget.GITLAB, CiExecutionMode.SERIAL),
                CiTargetPreference(CiTarget.LOCAL, CiExecutionMode.PARALLEL),
            )
        )

        self.assertEqual(ProjectDefinition.from_dict(definition.to_dict()), definition)

    def test_github_and_gitlab_together_are_rejected(self) -> None:
        with self.assertRaises(ContractValidationError):
            _definition(
                ci_targets=(
                    CiTargetPreference(CiTarget.GITHUB, CiExecutionMode.SERIAL),
                    CiTargetPreference(CiTarget.GITLAB, CiExecutionMode.SERIAL),
                )
            )

    def test_duplicate_target_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError):
            _definition(
                ci_targets=(
                    CiTargetPreference(CiTarget.LOCAL, CiExecutionMode.SERIAL),
                    CiTargetPreference(CiTarget.LOCAL, CiExecutionMode.PARALLEL),
                )
            )

    def test_untyped_entry_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError):
            _definition(ci_targets=({"target": "local", "mode": "serial"},))

    def test_project_type_is_optional_and_round_trips(self) -> None:
        definition = _definition()
        self.assertIsNone(definition.project_type)
        self.assertNotIn("project_type", definition.to_dict())
        typed = _definition(project_type="library")
        self.assertEqual(typed.to_dict()["project_type"], "library")
        self.assertEqual(ProjectDefinition.from_dict(typed.to_dict()), typed)
        with self.assertRaises(ContractValidationError):
            _definition(project_type="spaceship")


if __name__ == "__main__":
    unittest.main()
