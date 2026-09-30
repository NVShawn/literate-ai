"""Fail-closed review service for model-proposed semantic refinements."""

from __future__ import annotations

from literate_ai.contracts.semantic_refinement import (
    AcceptedSemanticRefinement,
    ReviewDecision,
    SemanticConflict,
    SemanticRefinementProposal,
    SemanticRefinementRequest,
    SemanticRefinementResolution,
    SemanticRefinementReview,
)


class SemanticRefinementError(ValueError):
    """A refinement set lacks complete exact acceptance evidence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SemanticRefinementService:
    """Resolve only a complete one-conflict/one-proposal/one-review closure.

    This service performs identity and set-closure checks.  It intentionally performs
    no textual entailment, ancestry precedence, or "narrow never contradicts" proof.
    """

    def resolve(
        self, request: SemanticRefinementRequest
    ) -> SemanticRefinementResolution:
        documents = {item.uri: item for item in request.documents}
        conflicts = {item.conflict_id: item for item in request.conflicts}
        proposals = {item.proposal_id: item for item in request.proposals}

        if not conflicts and (proposals or request.reviews):
            raise SemanticRefinementError(
                "semantic_refinement.extra_evidence",
                "proposals or reviews exist without a declared conflict",
            )
        proposal_by_conflict = self._one_proposal_per_conflict(conflicts, proposals)
        review_by_proposal = self._one_review_per_proposal(proposals, request.reviews)

        accepted: list[AcceptedSemanticRefinement] = []
        for conflict_id in sorted(conflicts):
            conflict = conflicts[conflict_id]
            self._check_conflict_closure(conflict, documents)
            proposal = proposal_by_conflict[conflict_id]
            if proposal.conflict_identity != conflict.identity:
                raise SemanticRefinementError(
                    "semantic_refinement.proposal_binding_mismatch",
                    f"proposal {proposal.proposal_id!r} does not bind conflict bytes",
                )
            review = review_by_proposal[proposal.proposal_id]
            self._check_review_binding(conflict, proposal, review)
            if review.decision is ReviewDecision.REJECT:
                raise SemanticRefinementError(
                    "semantic_refinement.review_rejected",
                    f"review {review.review_id!r} rejected proposal "
                    f"{proposal.proposal_id!r}",
                )
            accepted.append(
                AcceptedSemanticRefinement(
                    conflict_id=conflict.conflict_id,
                    conflict_identity=conflict.identity,
                    proposal_id=proposal.proposal_id,
                    proposal_identity=proposal.identity,
                    review_id=review.review_id,
                    review_identity=review.identity,
                )
            )

        return SemanticRefinementResolution(
            request_identity=request.identity,
            document_closure=tuple(
                sorted(
                    (item.identity for item in request.documents),
                    key=lambda item: item.uri,
                )
            ),
            accepted=tuple(accepted),
        )

    @staticmethod
    def _one_proposal_per_conflict(
        conflicts: dict[str, SemanticConflict],
        proposals: dict[str, SemanticRefinementProposal],
    ) -> dict[str, SemanticRefinementProposal]:
        grouped: dict[str, list[SemanticRefinementProposal]] = {}
        for proposal in proposals.values():
            if proposal.conflict_id not in conflicts:
                raise SemanticRefinementError(
                    "semantic_refinement.extra_proposal",
                    f"proposal {proposal.proposal_id!r} names no declared conflict",
                )
            grouped.setdefault(proposal.conflict_id, []).append(proposal)
        for conflict_id in conflicts:
            count = len(grouped.get(conflict_id, ()))
            if count != 1:
                raise SemanticRefinementError(
                    "semantic_refinement.proposal_closure",
                    f"conflict {conflict_id!r} requires exactly one proposal; "
                    f"found {count}",
                )
        return {key: value[0] for key, value in grouped.items()}

    @staticmethod
    def _one_review_per_proposal(
        proposals: dict[str, SemanticRefinementProposal],
        reviews: tuple[SemanticRefinementReview, ...],
    ) -> dict[str, SemanticRefinementReview]:
        grouped: dict[str, list[SemanticRefinementReview]] = {}
        for review in reviews:
            if review.proposal_id not in proposals:
                raise SemanticRefinementError(
                    "semantic_refinement.extra_review",
                    f"review {review.review_id!r} names no declared proposal",
                )
            grouped.setdefault(review.proposal_id, []).append(review)
        for proposal_id in proposals:
            count = len(grouped.get(proposal_id, ()))
            if count != 1:
                raise SemanticRefinementError(
                    "semantic_refinement.review_closure",
                    f"proposal {proposal_id!r} requires exactly one review; "
                    f"found {count}",
                )
        return {key: value[0] for key, value in grouped.items()}

    @staticmethod
    def _check_conflict_closure(
        conflict: SemanticConflict, documents: dict[str, object]
    ) -> None:
        for document in conflict.documents:
            selected = documents.get(document.uri)
            if selected != document:
                raise SemanticRefinementError(
                    "semantic_refinement.document_outside_closure",
                    f"conflict {conflict.conflict_id!r} does not bind the exact "
                    f"declared document {document.uri!r}",
                )

    @staticmethod
    def _check_review_binding(
        conflict: SemanticConflict,
        proposal: SemanticRefinementProposal,
        review: SemanticRefinementReview,
    ) -> None:
        if review.conflict_id != conflict.conflict_id:
            raise SemanticRefinementError(
                "semantic_refinement.review_conflict_mismatch",
                f"review {review.review_id!r} names the wrong conflict",
            )
        binding = review.binding
        expected_documents = tuple(
            sorted(
                (item.identity for item in conflict.documents),
                key=lambda item: item.uri,
            )
        )
        reviewed_documents = tuple(
            sorted(binding.document_identities, key=lambda item: item.uri)
        )
        expected_evidence = tuple(
            sorted(
                (item.identity for item in conflict.evidence), key=lambda item: item.uri
            )
        )
        reviewed_evidence = tuple(
            sorted(binding.evidence_identities, key=lambda item: item.uri)
        )
        if binding.proposal_identity != proposal.identity:
            code = "semantic_refinement.review_proposal_mismatch"
        elif reviewed_documents != expected_documents:
            code = "semantic_refinement.review_documents_mismatch"
        elif binding.model_identity != conflict.model.identity:
            code = "semantic_refinement.review_model_mismatch"
        elif reviewed_evidence != expected_evidence:
            code = "semantic_refinement.review_evidence_mismatch"
        else:
            return
        raise SemanticRefinementError(
            code,
            f"review {review.review_id!r} is not bound to the exact proposal inputs",
        )


__all__ = ["SemanticRefinementError", "SemanticRefinementService"]
