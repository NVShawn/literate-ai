"""Adversarial evidence tests for reviewed semantic refinement."""

from __future__ import annotations

import dataclasses
import unittest

from literate_ai.application.semantic_refinement import (
    SemanticRefinementError,
    SemanticRefinementService,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.identity import ContentReference, canonical_identity
from literate_ai.contracts.semantic_refinement import (
    ProposalVerification,
    RefinementReviewBinding,
    ReviewDecision,
    SemanticConflict,
    SemanticRefinementProposal,
    SemanticRefinementRequest,
    SemanticRefinementReview,
)


def _reference(kind: str, uri: str, content: str) -> ContentReference:
    return ContentReference(kind, uri, canonical_identity(content))


def _fixture() -> SemanticRefinementRequest:
    root = _reference("specification-document", "spec/root.md", "root bytes")
    child = _reference("specification-document", "spec/child.md", "child bytes")
    model = _reference("coding-model", "model://codex/example", "route and model")
    evidence = _reference("model-evidence", "journal://turn/7", "prompt response")
    conflict = SemanticConflict(
        conflict_id="retention-conflict",
        documents=(child, root),
        model=model,
        evidence=(evidence,),
        description="The child requests 90 days while the root caps retention at 30.",
    )
    proposal = SemanticRefinementProposal(
        proposal_id="retention-refinement",
        conflict_id=conflict.conflict_id,
        conflict_identity=conflict.identity,
        replacement="The child retains records for no more than 30 days.",
    )
    review = SemanticRefinementReview(
        review_id="review-retention-v1",
        conflict_id=conflict.conflict_id,
        proposal_id=proposal.proposal_id,
        binding=RefinementReviewBinding(
            proposal_identity=proposal.identity,
            document_identities=tuple(item.identity for item in conflict.documents),
            model_identity=model.identity,
            evidence_identities=(evidence.identity,),
        ),
        reviewer=_reference("reviewer", "person://reviewer/alex", "alex key v1"),
        decision=ReviewDecision.ACCEPT,
        rationale="The root policy is authoritative; the replacement preserves it.",
    )
    return SemanticRefinementRequest(
        documents=(child, root),
        conflicts=(conflict,),
        proposals=(proposal,),
        reviews=(review,),
    )


class SemanticRefinementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SemanticRefinementService()

    def assert_code(self, expected: str, request: SemanticRefinementRequest) -> None:
        with self.assertRaises(SemanticRefinementError) as caught:
            self.service.resolve(request)
        self.assertEqual(caught.exception.code, expected)

    def test_model_proposal_can_only_be_unverified(self) -> None:
        request = _fixture()
        value = request.proposals[0].to_dict()
        value["verification"] = "accepted"

        with self.assertRaises(ContractValidationError):
            SemanticRefinementProposal.from_dict(value)

        conflict = request.conflicts[0].to_dict()
        conflict["verification"] = "accepted"
        with self.assertRaises(ContractValidationError):
            SemanticConflict.from_dict(conflict)

        self.assertEqual(
            request.proposals[0].verification, ProposalVerification.UNVERIFIED
        )
        self.assertEqual(
            request.conflicts[0].verification, ProposalVerification.UNVERIFIED
        )

    def test_rejected_review_is_evidence_but_never_authority(self) -> None:
        request = _fixture()
        rejected = dataclasses.replace(
            request.reviews[0],
            decision=ReviewDecision.REJECT,
            rationale="The proposed text omits an exception.",
        )
        self.assert_code(
            "semantic_refinement.review_rejected",
            dataclasses.replace(request, reviews=(rejected,)),
        )


if __name__ == "__main__":
    unittest.main()
