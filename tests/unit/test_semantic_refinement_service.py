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
    SemanticRefinementResolution,
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

    def test_exact_acceptance_produces_deterministic_closed_resolution(self) -> None:
        request = _fixture()
        result = self.service.resolve(request)
        reordered = dataclasses.replace(
            request,
            documents=tuple(reversed(request.documents)),
        )

        self.assertEqual(result, self.service.resolve(reordered))
        self.assertEqual(result.request_identity, request.identity)
        self.assertEqual(
            SemanticRefinementResolution.from_dict(result.to_dict()), result
        )
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.accepted[0].proposal_id, "retention-refinement")
        self.assertEqual(
            result.document_closure,
            tuple(
                sorted(
                    (item.identity for item in request.documents),
                    key=lambda item: item.uri,
                )
            ),
        )

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

    def test_missing_and_duplicate_proposals_fail_closed(self) -> None:
        request = _fixture()
        self.assert_code(
            "semantic_refinement.proposal_closure",
            dataclasses.replace(request, proposals=()),
        )
        second = dataclasses.replace(
            request.proposals[0], proposal_id="alternative-refinement"
        )
        self.assert_code(
            "semantic_refinement.proposal_closure",
            dataclasses.replace(request, proposals=(*request.proposals, second)),
        )

    def test_missing_duplicate_and_extra_reviews_fail_closed(self) -> None:
        request = _fixture()
        self.assert_code(
            "semantic_refinement.review_closure",
            dataclasses.replace(request, reviews=()),
        )
        duplicate = dataclasses.replace(request.reviews[0], review_id="second-review")
        self.assert_code(
            "semantic_refinement.review_closure",
            dataclasses.replace(request, reviews=(*request.reviews, duplicate)),
        )
        extra = dataclasses.replace(
            request.reviews[0], review_id="extra-review", proposal_id="unknown-proposal"
        )
        self.assert_code(
            "semantic_refinement.extra_review",
            dataclasses.replace(request, reviews=(*request.reviews, extra)),
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

    def test_review_must_bind_exact_proposal_bytes(self) -> None:
        request = _fixture()
        changed = dataclasses.replace(
            request.proposals[0], replacement="Plausible but unreviewed replacement."
        )
        review = dataclasses.replace(
            request.reviews[0], proposal_id=changed.proposal_id
        )
        self.assert_code(
            "semantic_refinement.review_proposal_mismatch",
            dataclasses.replace(request, proposals=(changed,), reviews=(review,)),
        )

    def test_review_must_bind_exact_document_model_and_evidence_bytes(self) -> None:
        request = _fixture()
        review = request.reviews[0]
        mutations = (
            (
                "semantic_refinement.review_documents_mismatch",
                dataclasses.replace(
                    review.binding,
                    document_identities=(
                        *review.binding.document_identities[:-1],
                        canonical_identity("changed document"),
                    ),
                ),
            ),
            (
                "semantic_refinement.review_model_mismatch",
                dataclasses.replace(
                    review.binding, model_identity=canonical_identity("different model")
                ),
            ),
            (
                "semantic_refinement.review_evidence_mismatch",
                dataclasses.replace(
                    review.binding,
                    evidence_identities=(canonical_identity("different evidence"),),
                ),
            ),
        )
        for code, binding in mutations:
            with self.subTest(code=code):
                self.assert_code(
                    code,
                    dataclasses.replace(
                        request, reviews=(dataclasses.replace(review, binding=binding),)
                    ),
                )

    def test_conflict_cannot_cite_document_outside_exact_request_closure(self) -> None:
        request = _fixture()
        changed_document = dataclasses.replace(
            request.documents[0], identity=canonical_identity("changed bytes")
        )
        self.assert_code(
            "semantic_refinement.document_outside_closure",
            dataclasses.replace(
                request, documents=(changed_document, request.documents[1])
            ),
        )

    def test_no_nearest_document_wins_or_implicit_conflict_resolution(self) -> None:
        request = _fixture()
        first = request.conflicts[0]
        second = dataclasses.replace(first, conflict_id="nested-retention-conflict")

        # Adding a nearer/nested declaration does not override the first conflict.
        # Both conflicts require their own exact proposal and review.
        self.assert_code(
            "semantic_refinement.proposal_closure",
            dataclasses.replace(request, conflicts=(first, second)),
        )

    def test_contracts_reject_unknown_fields_and_round_trip_exactly(self) -> None:
        request = _fixture()
        self.assertEqual(
            SemanticRefinementRequest.from_dict(request.to_dict()).to_dict(),
            request.to_dict(),
        )
        value = request.to_dict()
        value["implicit_precedence"] = "nearest-wins"
        with self.assertRaises(ContractValidationError):
            SemanticRefinementRequest.from_dict(value)


if __name__ == "__main__":
    unittest.main()
