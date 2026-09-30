"""Versioned contract tests for Standard stage checkpoints and retry lineage."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from literate_ai.contracts import (
    BlobRef,
    CachedSourceFile,
    ContractValidationError,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    StandardLifecycleAttemptEvidence,
    StandardLifecycleCheckpoint,
    StandardLifecycleCheckpointOutcome,
    StandardLifecycleStage,
    StandardLifecycleStageEvidence,
    canonical_identity,
)
from tests.unit.test_schema_catalog import SchemaCatalog
from tests.unit.test_source_generation_boundary_contracts import (
    _candidate,
    _identity,
    _provenance,
)


def _evidence(
    *,
    stage: StandardLifecycleStage = StandardLifecycleStage.SOURCE_GENERATION,
    outcome: StandardLifecycleCheckpointOutcome = (
        StandardLifecycleCheckpointOutcome.PASSED
    ),
) -> StandardLifecycleStageEvidence:
    candidate = _candidate()
    provenance = _provenance(candidate)
    output = SourceGenerationRunOutput(
        candidate, candidate.identity, provenance, provenance.identity
    )
    resume = SourceGenerationResumeCandidate(
        output, output.identity, _identity("budget"), _identity("decision")
    )
    return StandardLifecycleStageEvidence(
        _identity("execution-plan"),
        candidate.component_revision,
        candidate.component_generation_plan_identity,
        candidate.generation_key_identity,
        candidate.recipe_identity,
        resume,
        stage,
        canonical_identity({"stage-subject": stage.value}),
        outcome,
        "test.failed" if outcome is StandardLifecycleCheckpointOutcome.FAILED else None,
    )


def _source_file() -> CachedSourceFile:
    content = b"source"
    return CachedSourceFile(
        "source/main.py",
        BlobRef(hashlib.sha256(content).hexdigest(), len(content)),
    )


class StandardLifecycleCheckpointContractTests(unittest.TestCase):
    def test_stage_and_checkpoint_round_trip_through_public_schema(self) -> None:
        evidence = _evidence()
        attempt = StandardLifecycleAttemptEvidence(
            evidence.execution_plan_identity,
            evidence.component_revision,
            evidence.generation_plan_identity,
            evidence.generation_key_identity,
            evidence.recipe_identity,
            1,
            None,
            None,
        )
        first = StandardLifecycleCheckpoint(
            1, attempt, 1, 1, None, evidence, (_source_file(),)
        )
        retry = StandardLifecycleAttemptEvidence(
            evidence.execution_plan_identity,
            evidence.component_revision,
            evidence.generation_plan_identity,
            evidence.generation_key_identity,
            evidence.recipe_identity,
            2,
            attempt.identity,
            first.identity,
        )
        second = StandardLifecycleCheckpoint(
            2,
            retry,
            1,
            2,
            first.identity,
            _evidence(
                stage=StandardLifecycleStage.TEST,
                outcome=StandardLifecycleCheckpointOutcome.FAILED,
            ),
            first.source_files,
        )
        catalog = SchemaCatalog()

        for value, parser in (
            (attempt, StandardLifecycleAttemptEvidence.from_dict),
            (retry, StandardLifecycleAttemptEvidence.from_dict),
            (evidence, StandardLifecycleStageEvidence.from_dict),
            (first, StandardLifecycleCheckpoint.from_dict),
            (second, StandardLifecycleCheckpoint.from_dict),
        ):
            with self.subTest(contract=type(value).__name__):
                document = value.to_dict()
                catalog.validate(value.SCHEMA, document)
                restored = parser(document)
                self.assertEqual(restored, value)
                self.assertEqual(restored.identity, value.identity)

    def test_outcome_and_predecessor_fields_fail_closed(self) -> None:
        with self.assertRaises(ContractValidationError):
            StandardLifecycleCheckpoint(
                1,
                StandardLifecycleAttemptEvidence(
                    _identity("execution-plan"),
                    _identity("component"),
                    _identity("plan"),
                    _identity("key"),
                    _identity("recipe"),
                    1,
                    None,
                    None,
                ),
                1,
                2,
                None,
                _evidence(),
                (_source_file(),),
            )
        with self.assertRaises(ContractValidationError):
            replace(_evidence(), failure_code="unexpected")


if __name__ == "__main__":
    unittest.main()
