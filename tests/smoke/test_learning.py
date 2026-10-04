"""Evidence-only learning observations and deterministic owner classification."""

from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from literate_ai.cli import main
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.learning import (
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
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


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
