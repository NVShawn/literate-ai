from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_semantic_refinement_service``."""


from literate_ai.contracts.identity import ContentReference, canonical_identity
from literate_ai.contracts.semantic_refinement import (
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
