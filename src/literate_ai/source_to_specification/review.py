"""Explicit review and atomic in-memory promotion policy."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    ComponentGraphDraft,
    CoverageState,
    DraftArtifact,
    DraftStatement,
    ProviderValidation,
    ReviewAuthority,
    ReviewDisposition,
    SourceToSpecificationResult,
    canonical_digest,
)
from .errors import SourceToSpecificationError


@dataclass(frozen=True, slots=True)
class ReviewStatementDecision:
    statement_id: str
    disposition: ReviewDisposition
    reason: str
    replacement: DraftStatement | None = None

    def __post_init__(self) -> None:
        if not self.statement_id.strip() or not self.reason.strip():
            raise SourceToSpecificationError(
                "review.incomplete_decision",
                "review statement ID and reason must not be empty",
            )
        if self.disposition in {ReviewDisposition.EDIT, ReviewDisposition.SUPERSEDE}:
            if self.replacement is None:
                raise SourceToSpecificationError(
                    "review.replacement_required",
                    f"{self.disposition.value} requires a replacement statement",
                )
        elif self.replacement is not None:
            raise SourceToSpecificationError(
                "review.unexpected_replacement",
                f"{self.disposition.value} cannot carry a replacement statement",
            )


@dataclass(frozen=True, slots=True)
class SpecificationReviewDecision:
    review_id: str
    draft_id: str
    base_specification_set_id: str | None
    actor: str
    authority: ReviewAuthority
    reason: str
    statement_decisions: tuple[ReviewStatementDecision, ...]
    resolved_uncertainty_ids: tuple[str, ...]
    resulting_artifacts: tuple[DraftArtifact, ...]
    validation: ProviderValidation
    component_graph_draft: ComponentGraphDraft | None = None

    def __post_init__(self) -> None:
        for name in ("review_id", "draft_id", "actor", "reason"):
            if not getattr(self, name).strip():
                raise SourceToSpecificationError(
                    "review.empty_field", f"{name} must not be empty"
                )
        ids = tuple(item.statement_id for item in self.statement_decisions)
        if len(ids) != len(set(ids)):
            raise SourceToSpecificationError(
                "review.duplicate_statement", "each statement requires one decision"
            )
        if len(self.resolved_uncertainty_ids) != len(
            set(self.resolved_uncertainty_ids)
        ):
            raise SourceToSpecificationError(
                "review.duplicate_uncertainty",
                "resolved uncertainty IDs must be unique",
            )
        paths = tuple(item.path for item in self.resulting_artifacts)
        if not paths or len(paths) != len(set(paths)):
            raise SourceToSpecificationError(
                "review.invalid_result_tree",
                "the complete resulting spec tree requires unique artifacts",
            )
        if self.component_graph_draft is not None and not isinstance(
            self.component_graph_draft, ComponentGraphDraft
        ):
            raise SourceToSpecificationError(
                "review.component_graph_invalid",
                "reviewed Component graph must be a typed ComponentGraphDraft",
            )


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    policy_id: str
    allowed_authorities: tuple[ReviewAuthority, ...] = (ReviewAuthority.HUMAN,)
    allowed_coverage_states: tuple[CoverageState, ...] = (
        CoverageState.COVERED,
        CoverageState.EXCLUDED,
        CoverageState.IMPLEMENTATION_DETAIL,
    )
    allow_deferred: bool = False
    require_all_blocking_uncertainty_resolved: bool = True

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.allowed_authorities:
            raise SourceToSpecificationError(
                "promotion.invalid_policy", "promotion policy is incomplete"
            )


@dataclass(frozen=True, slots=True)
class SpecificationSet:
    """Reviewed intent derived from source, not yet release implementation authority."""

    specification_set_id: str
    source_snapshot_id: str
    provider: str
    artifacts: tuple[DraftArtifact, ...]
    statements: tuple[DraftStatement, ...]
    source_draft_id: str
    review_id: str
    base_specification_set_id: str | None
    promotion_policy_id: str

    @property
    def identity(self) -> str:
        return self.specification_set_id


def promote(
    result: SourceToSpecificationResult,
    review: SpecificationReviewDecision,
    policy: PromotionPolicy,
    *,
    expected_base_specification_set_id: str | None,
) -> SpecificationSet:
    """Return intent authority only after complete atomic review validation."""

    draft = result.draft
    if review.draft_id != draft.draft_id:
        raise SourceToSpecificationError(
            "promotion.draft_mismatch", "review does not target the supplied draft"
        )
    if review.base_specification_set_id != expected_base_specification_set_id:
        raise SourceToSpecificationError(
            "promotion.base_drift", "accepted specification base changed during review"
        )
    if review.authority not in policy.allowed_authorities:
        raise SourceToSpecificationError(
            "promotion.authority_denied", "review authority is not permitted by policy"
        )
    if not draft.validation.valid or not review.validation.valid:
        raise SourceToSpecificationError(
            "promotion.validation_failed", "draft and resulting tree must both validate"
        )
    if review.validation.provider != draft.output_provider:
        raise SourceToSpecificationError(
            "promotion.provider_mismatch",
            "resulting tree was not validated by the selected provider",
        )
    expected_statement_ids = {item.statement_id for item in draft.statements}
    decisions = {item.statement_id: item for item in review.statement_decisions}
    if decisions.keys() != expected_statement_ids:
        raise SourceToSpecificationError(
            "promotion.incomplete_review",
            "every proposed statement requires exactly one review disposition",
        )
    if not policy.allow_deferred and any(
        item.disposition is ReviewDisposition.DEFER for item in decisions.values()
    ):
        raise SourceToSpecificationError(
            "promotion.deferred_statement", "deferred statements block promotion"
        )
    disallowed_coverage = {item.state for item in result.coverage.entries} - set(
        policy.allowed_coverage_states
    )
    if disallowed_coverage:
        raise SourceToSpecificationError(
            "promotion.coverage_blocked",
            "coverage policy blocks: "
            + ", ".join(sorted(item.value for item in disallowed_coverage)),
        )
    if policy.require_all_blocking_uncertainty_resolved:
        blocking = {
            item.uncertainty_id for item in result.uncertainty.items if item.blocking
        }
        unresolved = blocking - set(review.resolved_uncertainty_ids)
        if unresolved:
            raise SourceToSpecificationError(
                "promotion.uncertainty_unresolved",
                f"blocking uncertainty remains: {', '.join(sorted(unresolved))}",
            )

    original = {item.statement_id: item for item in draft.statements}
    accepted: list[DraftStatement] = []
    for statement_id in sorted(decisions):
        decision = decisions[statement_id]
        if decision.disposition is ReviewDisposition.ACCEPT:
            accepted.append(original[statement_id])
        elif decision.disposition in {
            ReviewDisposition.EDIT,
            ReviewDisposition.SUPERSEDE,
        }:
            if decision.replacement is None:  # Defensive against untrusted decoders.
                raise SourceToSpecificationError(
                    "promotion.replacement_missing",
                    "edited or superseded statements require replacements",
                )
            accepted.append(decision.replacement)
    identity_payload = {
        "source_snapshot_id": result.request.source_snapshot_id,
        "provider": draft.output_provider,
        "artifacts": review.resulting_artifacts,
        "statements": tuple(accepted),
        "source_draft_id": draft.draft_id,
        "review_id": review.review_id,
        "base_specification_set_id": review.base_specification_set_id,
        "promotion_policy_id": policy.policy_id,
    }
    specification_set_id = canonical_digest(identity_payload)
    return SpecificationSet(
        specification_set_id=specification_set_id,
        source_snapshot_id=result.request.source_snapshot_id,
        provider=draft.output_provider,
        artifacts=review.resulting_artifacts,
        statements=tuple(accepted),
        source_draft_id=draft.draft_id,
        review_id=review.review_id,
        base_specification_set_id=review.base_specification_set_id,
        promotion_policy_id=policy.policy_id,
    )


__all__ = [
    "PromotionPolicy",
    "ReviewStatementDecision",
    "SpecificationReviewDecision",
    "SpecificationSet",
    "promote",
]
