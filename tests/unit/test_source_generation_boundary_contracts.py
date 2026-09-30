"""Focused conformance tests for the additive source-generation boundary."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator

from literate_ai.contracts import (
    GeneratedSourceCandidate,
    SourceGenerationCheckpoint,
    SourceGenerationCheckpointStatus,
    SourceGenerationProvenance,
    SourceGenerationRunOutput,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.executable_components import (
    ComponentGenerationRuntimeObservation,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

ROOT = Path(__file__).parents[2]
SCHEMA = ROOT / "schemas" / "v2" / "source-generation-boundary.schema.json"


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def _candidate() -> GeneratedSourceCandidate:
    return GeneratedSourceCandidate(
        component_revision=_identity("component"),
        source_generation_request_identity=_identity("orchestration-request"),
        planned_coding_cli_request_identity=_identity("planned-coding-cli-request"),
        component_generation_plan_identity=_identity("plan"),
        generation_key_identity=_identity("key"),
        context_manifest_identity=_identity("context"),
        prompt_identity=_identity("prompt"),
        recipe_identity=_identity("recipe"),
        workspace_allocation_identity=_identity("workspace"),
        tree_identity=_identity("tree"),
        source_bundle_identity=_identity("bundle"),
        source_manifest_identity=_identity("manifest"),
        source_bom_identity=_identity("bom"),
        generated_test_suite_identity=_identity("generated-test-suite"),
    )


def _provenance(candidate: GeneratedSourceCandidate) -> SourceGenerationProvenance:
    return SourceGenerationProvenance(
        source_generation_request_identity=(
            candidate.source_generation_request_identity
        ),
        planned_coding_cli_request_identity=(
            candidate.planned_coding_cli_request_identity
        ),
        component_lock_identity=_identity("lock"),
        application_root_revision_identity=_identity("application-root"),
        generated_component_revision_identity=candidate.component_revision,
        component_generation_plan_identity=(
            candidate.component_generation_plan_identity
        ),
        generation_key_identity=candidate.generation_key_identity,
        context_manifest_identity=candidate.context_manifest_identity,
        prompt_identity=candidate.prompt_identity,
        recipe_identity=candidate.recipe_identity,
        workspace_allocation_identity=candidate.workspace_allocation_identity,
        readiness_identity=_identity("readiness"),
        route_decision_identities=(_identity("route-plan"), _identity("route-code")),
        model_stage_output_identities=(
            _identity("output-plan"),
            _identity("output-code"),
        ),
        candidate_identity=candidate.identity,
    )


class SourceGenerationBoundaryContractTests(unittest.TestCase):
    def test_retained_provenance_wire_has_no_model_claims(self):
        provenance = replace(
            _provenance(_candidate()),
            route_decision_identities=(),
            model_stage_output_identities=(),
            retained_source_identity=_identity("retained-input"),
        )
        self.assert_wire_round_trip(provenance, SourceGenerationProvenance.from_dict)
        with self.assertRaises(ContractValidationError):
            replace(provenance, retained_source_identity=None)

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def assert_wire_round_trip(self, value, parser) -> None:
        document = value.to_dict()
        self.validator.validate(document)
        restored = parser(document)
        self.assertEqual(restored, value)
        self.assertEqual(restored.identity, value.identity)

    def test_all_contracts_round_trip_with_canonical_identities(self) -> None:
        candidate = _candidate()
        provenance = _provenance(candidate)
        output = SourceGenerationRunOutput(
            candidate,
            candidate.identity,
            provenance,
            provenance.identity,
            ComponentGenerationRuntimeObservation(1, 200, 300, None),
        )
        checkpoint = SourceGenerationCheckpoint(
            "source-generation:fixture",
            candidate.source_generation_request_identity,
            SourceGenerationCheckpointStatus.CANDIDATE_READY,
            _identity("event-stream"),
            ("plan", "generate"),
            provenance.model_stage_output_identities,
            candidate.identity,
            provenance.identity,
        )

        for value, parser in (
            (candidate, GeneratedSourceCandidate.from_dict),
            (provenance, SourceGenerationProvenance.from_dict),
            (output, SourceGenerationRunOutput.from_dict),
            (checkpoint, SourceGenerationCheckpoint.from_dict),
        ):
            with self.subTest(contract=type(value).__name__):
                self.assert_wire_round_trip(value, parser)

    def test_candidate_and_output_fail_closed_on_identity_drift(self) -> None:
        candidate = _candidate()
        provenance = _provenance(candidate)
        with self.assertRaises(ContractValidationError):
            SourceGenerationRunOutput(
                candidate,
                _identity("another-candidate"),
                provenance,
                provenance.identity,
            )
        with self.assertRaises(ContractValidationError):
            SourceGenerationRunOutput(
                candidate,
                candidate.identity,
                replace(
                    provenance,
                    candidate_identity=_identity("another-candidate"),
                ),
                provenance.identity,
            )

    def test_provenance_rejects_incomplete_or_duplicated_model_evidence(self) -> None:
        candidate = _candidate()
        provenance = _provenance(candidate)
        with self.assertRaises(ContractValidationError):
            replace(provenance, model_stage_output_identities=())
        with self.assertRaises(ContractValidationError):
            replace(
                provenance,
                route_decision_identities=(
                    provenance.route_decision_identities[0],
                    provenance.route_decision_identities[0],
                ),
            )
        with self.assertRaises(ContractValidationError):
            replace(
                provenance,
                model_stage_output_identities=(
                    provenance.model_stage_output_identities[0],
                ),
            )

    def test_checkpoint_terminal_fields_are_status_exact(self) -> None:
        candidate = _candidate()
        provenance = _provenance(candidate)
        common = (
            "source-generation:fixture",
            candidate.source_generation_request_identity,
        )
        with self.assertRaises(ContractValidationError):
            SourceGenerationCheckpoint(
                *common,
                SourceGenerationCheckpointStatus.RUNNING,
                _identity("events"),
                (),
                (),
                candidate.identity,
                provenance.identity,
            )
        with self.assertRaises(ContractValidationError):
            SourceGenerationCheckpoint(
                *common,
                SourceGenerationCheckpointStatus.PAUSED,
                _identity("events"),
                (),
                (),
                candidate_identity=candidate.identity,
            )
        with self.assertRaises(ContractValidationError):
            SourceGenerationCheckpoint(
                *common,
                SourceGenerationCheckpointStatus.FAILED,
                _identity("events"),
                (),
                (),
            )
        failed = SourceGenerationCheckpoint(
            *common,
            SourceGenerationCheckpointStatus.FAILED,
            _identity("events"),
            (),
            (),
            failure_code="generation.model-failed",
        )
        self.assert_wire_round_trip(failed, SourceGenerationCheckpoint.from_dict)

    def test_from_dict_rejects_unknown_missing_and_wrong_schema_fields(self) -> None:
        document = _candidate().to_dict()
        unknown = copy.deepcopy(document)
        unknown["authorization"] = _identity("forbidden").to_dict()
        missing = copy.deepcopy(document)
        del missing["tree_identity"]
        wrong_schema = copy.deepcopy(document)
        wrong_schema["schema"] = "urn:literate-ai:schema:v3:not-this-contract"

        for malformed in (unknown, missing, wrong_schema):
            with self.subTest(fields=tuple(sorted(malformed))):
                with self.assertRaises(ContractValidationError):
                    GeneratedSourceCandidate.from_dict(malformed)
                self.assertFalse(self.validator.is_valid(malformed))

    def test_wire_vocabulary_contains_no_post_generation_authority(self) -> None:
        candidate = _candidate()
        provenance = _provenance(candidate)
        documents = (
            candidate.to_dict(),
            provenance.to_dict(),
            SourceGenerationRunOutput(
                candidate,
                candidate.identity,
                provenance,
                provenance.identity,
            ).to_dict(),
            SourceGenerationCheckpoint(
                "source-generation:fixture",
                candidate.source_generation_request_identity,
                SourceGenerationCheckpointStatus.CANDIDATE_READY,
                _identity("events"),
                ("generate",),
                (_identity("model-output"),),
                candidate.identity,
                provenance.identity,
            ).to_dict(),
        )
        forbidden = {
            "build_request",
            "authorization",
            "authorization_identity",
            "provider_artifacts",
            "execution",
            "execution_identity",
            "test_result",
            "acceptance",
            "acceptance_identity",
        }

        def keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | {
                    nested for item in value.values() for nested in keys(item)
                }
            if isinstance(value, list):
                return {nested for item in value for nested in keys(item)}
            return set()

        for document in documents:
            with self.subTest(schema=document["schema"]):
                self.assertFalse(keys(document) & forbidden)


if __name__ == "__main__":
    unittest.main()
