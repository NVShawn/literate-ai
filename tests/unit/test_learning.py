"""Evidence-only learning observations and deterministic owner classification."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path

from literate_ai.application.learning import (
    DETERMINISTIC_LEARNING_CLASSIFIER_IDENTITY,
    LearningClassificationError,
    classify_learning_observation,
    plan_learning_proposal,
)
from literate_ai.cli import main
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.learning import (
    LearningDisposition,
    LearningEvidenceBinding,
    LearningEvidenceKind,
    LearningObservation,
    LearningObservationOutcome,
    LearningPlanInput,
    LearningProposal,
    LearningProposalScope,
    LearningSignalEvidence,
    LearningSignalKind,
)
from tests.unit.test_schema_catalog import SchemaCatalog


def _identity(label: str):
    return canonical_identity({"learning-fixture": label})


def _rejected_observation(*signals: LearningSignalKind) -> LearningObservation:
    derivation = LearningEvidenceBinding(
        LearningEvidenceKind.DERIVATION, _identity("derivation")
    )
    rejection = LearningEvidenceBinding(
        LearningEvidenceKind.REJECTION, _identity("rejection")
    )
    return LearningObservation(
        _identity("component"),
        _identity("project-result"),
        _identity("trusted-observer"),
        LearningObservationOutcome.REJECTED,
        (derivation, rejection),
        tuple(
            sorted(
                (
                    LearningSignalEvidence(signal, rejection.identity)
                    for signal in signals
                ),
                key=lambda item: (item.signal.value, item.evidence_identity.uri),
            )
        ),
        ("compiler.generated-declaration-rejected",),
    )


def _plan_input(signal: LearningSignalKind) -> LearningPlanInput:
    return LearningPlanInput(
        _rejected_observation(signal),
        (_identity("component-authority"),),
        (_identity("flavor-authority"),),
        (_identity("skill-authority"),),
        (_identity("workflow-authority"),),
        (_identity("routing-authority"),),
        (_identity("framework-authority"),),
        (_identity("generated-test-suite"),),
        (_identity("component"),),
    )


class LearningObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaCatalog()

    def test_observation_and_classification_round_trip_through_schema(self) -> None:
        observation = _rejected_observation(LearningSignalKind.TARGET_VARIANCE)
        classification = classify_learning_observation(observation)
        for value in (
            *observation.evidence,
            *observation.signal_evidence,
            observation,
            classification,
        ):
            with self.subTest(schema=value.SCHEMA):
                self.schemas.validate(value.SCHEMA, value.to_dict())
                decoded = type(value).from_dict(value.to_dict())
                self.assertEqual(decoded, value)
                if hasattr(value, "identity"):
                    self.assertEqual(decoded.identity, value.identity)
        self.assertEqual(
            classification.classifier_identity,
            DETERMINISTIC_LEARNING_CLASSIFIER_IDENTITY,
        )

    def test_classifier_covers_every_documented_disposition(self) -> None:
        cases = {
            LearningSignalKind.MISSING_BEHAVIOR: LearningDisposition.COMPONENT_BEHAVIOR,
            LearningSignalKind.MISSING_PUBLIC_INTERFACE: (
                LearningDisposition.COMPONENT_INTERFACE
            ),
            LearningSignalKind.TARGET_VARIANCE: LearningDisposition.FLAVOR_VARIANCE,
            LearningSignalKind.REUSABLE_CONVERSION_TECHNIQUE: (
                LearningDisposition.SKILL_TECHNIQUE
            ),
            LearningSignalKind.STAGE_OR_HANDOFF: LearningDisposition.WORKFLOW_HANDOFF,
            LearningSignalKind.ROUTING_ELIGIBILITY: (
                LearningDisposition.ROUTING_ELIGIBILITY
            ),
            LearningSignalKind.FRAMEWORK_DEFECT: LearningDisposition.FRAMEWORK_DEFECT,
            LearningSignalKind.AUTHORITY_ALREADY_FORBIDS: (
                LearningDisposition.CANDIDATE_SPECIFIC_NO_CHANGE
            ),
        }
        for signal, expected in cases.items():
            with self.subTest(signal=signal.value):
                first = classify_learning_observation(_rejected_observation(signal))
                second = classify_learning_observation(_rejected_observation(signal))
                self.assertEqual(first, second)
                self.assertIs(first.disposition, expected)
        self.assertIs(
            classify_learning_observation(_rejected_observation()).disposition,
            LearningDisposition.CANDIDATE_SPECIFIC_NO_CHANGE,
        )

    def test_cross_owner_signals_fail_closed_instead_of_guessing(self) -> None:
        with self.assertRaises(LearningClassificationError) as caught:
            classify_learning_observation(
                _rejected_observation(
                    LearningSignalKind.MISSING_BEHAVIOR,
                    LearningSignalKind.TARGET_VARIANCE,
                )
            )
        self.assertEqual(caught.exception.code, "learning.classification_ambiguous")

    def test_observation_shape_excludes_raw_or_private_values(self) -> None:
        observation = _rejected_observation(LearningSignalKind.FRAMEWORK_DEFECT)
        encoded = json.dumps(observation.to_dict(), sort_keys=True)
        for forbidden in (
            "PRIVATE-ORACLE-CANARY",
            "SECRET-TOKEN-CANARY",
            "/Users/person/source.cc",
            "C:\\Users\\person\\source.cc",
            "prompt",
            "diagnostic_text",
            "task_id",
            "conversation",
        ):
            self.assertNotIn(forbidden, encoded)
        with self.assertRaises(ValueError):
            replace(observation, fact_codes=("token=hunter2",))
        wire = observation.to_dict()
        wire["prompt"] = "PRIVATE-ORACLE-CANARY"
        with self.assertRaises(ValueError):
            LearningObservation.from_dict(wire)

    def test_evidence_outcome_and_signal_bindings_fail_closed(self) -> None:
        rejected = _rejected_observation(LearningSignalKind.MISSING_BEHAVIOR)
        with self.assertRaisesRegex(ValueError, "cannot claim acceptance"):
            replace(
                rejected,
                evidence=tuple(
                    sorted(
                        (
                            *rejected.evidence,
                            LearningEvidenceBinding(
                                LearningEvidenceKind.ACCEPTANCE,
                                _identity("acceptance"),
                            ),
                        ),
                        key=lambda item: (item.kind.value, item.identity.uri),
                    )
                ),
            )
        with self.assertRaisesRegex(ValueError, "retained exact evidence"):
            replace(
                rejected,
                signal_evidence=(
                    LearningSignalEvidence(
                        LearningSignalKind.MISSING_BEHAVIOR,
                        _identity("substituted-evidence"),
                    ),
                ),
            )

    def test_accepted_observation_requires_complete_success_chain(self) -> None:
        evidence = tuple(
            sorted(
                (
                    LearningEvidenceBinding(
                        LearningEvidenceKind.DERIVATION, _identity("derivation")
                    ),
                    LearningEvidenceBinding(
                        LearningEvidenceKind.BUILD, _identity("build")
                    ),
                    LearningEvidenceBinding(
                        LearningEvidenceKind.TEST, _identity("test")
                    ),
                    LearningEvidenceBinding(
                        LearningEvidenceKind.ACCEPTANCE, _identity("acceptance")
                    ),
                ),
                key=lambda item: (item.kind.value, item.identity.uri),
            )
        )
        observation = LearningObservation(
            _identity("component"),
            _identity("project-result"),
            _identity("trusted-observer"),
            LearningObservationOutcome.ACCEPTED,
            evidence,
            (),
            (),
        )
        self.schemas.validate(observation.SCHEMA, observation.to_dict())
        with self.assertRaisesRegex(ValueError, "require build, test, and acceptance"):
            replace(observation, evidence=evidence[:-1])

    def test_read_only_planner_selects_one_narrow_owner_and_test_scope(self) -> None:
        cases = {
            LearningSignalKind.MISSING_BEHAVIOR: LearningProposalScope.COMPONENT,
            LearningSignalKind.MISSING_PUBLIC_INTERFACE: (
                LearningProposalScope.COMPONENT
            ),
            LearningSignalKind.TARGET_VARIANCE: LearningProposalScope.FLAVOR,
            LearningSignalKind.REUSABLE_CONVERSION_TECHNIQUE: (
                LearningProposalScope.SKILL
            ),
            LearningSignalKind.STAGE_OR_HANDOFF: LearningProposalScope.WORKFLOW,
        }
        for signal, scope in cases.items():
            with self.subTest(signal=signal.value):
                plan_input = _plan_input(signal)
                proposal = plan_learning_proposal(plan_input)
                self.assertIs(proposal.scope, scope)
                self.assertEqual(len(proposal.authority_identities), 1)
                self.assertEqual(
                    proposal.affected_test_suite_identities,
                    plan_input.test_suite_identities,
                )
                self.assertEqual(proposal.plan_input_identity, plan_input.identity)
                self.schemas.validate(proposal.SCHEMA, proposal.to_dict())
                self.assertEqual(
                    LearningProposal.from_dict(proposal.to_dict()), proposal
                )

    def test_planner_rejects_ambiguous_or_missing_scope_authority(self) -> None:
        plan_input = _plan_input(LearningSignalKind.TARGET_VARIANCE)
        with self.assertRaisesRegex(
            LearningClassificationError, "exactly one pinned authority owner"
        ):
            plan_learning_proposal(
                replace(
                    plan_input,
                    flavor_authority_identities=(
                        *plan_input.flavor_authority_identities,
                        _identity("other-flavor"),
                    ),
                )
            )

    def test_litai_learn_emits_one_proposal_and_never_writes_authority(self) -> None:
        plan_input = _plan_input(LearningSignalKind.REUSABLE_CONVERSION_TECHNIQUE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run.json"
            run.write_text(json.dumps(plan_input.to_dict()), encoding="utf-8")
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            stdout = StringIO()
            stderr = StringIO()

            status = main(("--json", "learn", str(run)), stdout=stdout, stderr=stderr)

            self.assertEqual(status, 0, stderr.getvalue())
            envelope = json.loads(stdout.getvalue())
            self.assertEqual(envelope["command"], "learn")
            proposal = LearningProposal.from_dict(envelope["result"])
            self.assertIs(proposal.scope, LearningProposalScope.SKILL)
            self.assertNotIn("proposals", envelope["result"])
            after = {path.name: path.read_bytes() for path in root.iterdir()}
            self.assertEqual(after, before)

    def test_litai_learn_rejects_non_contract_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run.json"
            run.write_text('{"schema":"unknown"}', encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()
            status = main(("--json", "learn", str(run)), stdout=stdout, stderr=stderr)
            self.assertEqual(status, 2)
            error = json.loads(stderr.getvalue())
            self.assertFalse(error["ok"])


if __name__ == "__main__":
    unittest.main()
