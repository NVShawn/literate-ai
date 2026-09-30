"""Deterministic, evidence-only authority-owner classification."""

from __future__ import annotations

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.learning import (
    LearningClassification,
    LearningDisposition,
    LearningObservation,
    LearningPlanInput,
    LearningProposal,
    LearningProposalScope,
    LearningSignalKind,
)


class LearningClassificationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_DISPOSITIONS = {
    LearningSignalKind.MISSING_BEHAVIOR: LearningDisposition.COMPONENT_BEHAVIOR,
    LearningSignalKind.MISSING_PUBLIC_INTERFACE: (
        LearningDisposition.COMPONENT_INTERFACE
    ),
    LearningSignalKind.TARGET_VARIANCE: LearningDisposition.FLAVOR_VARIANCE,
    LearningSignalKind.REUSABLE_CONVERSION_TECHNIQUE: (
        LearningDisposition.SKILL_TECHNIQUE
    ),
    LearningSignalKind.STAGE_OR_HANDOFF: LearningDisposition.WORKFLOW_HANDOFF,
    LearningSignalKind.ROUTING_ELIGIBILITY: LearningDisposition.ROUTING_ELIGIBILITY,
    LearningSignalKind.FRAMEWORK_DEFECT: LearningDisposition.FRAMEWORK_DEFECT,
    LearningSignalKind.AUTHORITY_ALREADY_FORBIDS: (
        LearningDisposition.CANDIDATE_SPECIFIC_NO_CHANGE
    ),
}

DETERMINISTIC_LEARNING_CLASSIFIER_IDENTITY = canonical_identity(
    {
        "schema": "literate-ai/deterministic-learning-classifier@1",
        "mapping": {
            signal.value: disposition.value
            for signal, disposition in sorted(
                _DISPOSITIONS.items(), key=lambda item: item[0].value
            )
        },
        "empty_signal_disposition": (
            LearningDisposition.CANDIDATE_SPECIFIC_NO_CHANGE.value
        ),
        "ambiguity_policy": "fail-closed",
    }
)


def classify_learning_observation(
    observation: LearningObservation,
) -> LearningClassification:
    """Choose one narrow durable owner or fail closed on cross-owner ambiguity."""

    if not isinstance(observation, LearningObservation):
        raise TypeError("observation must be a LearningObservation")
    signals = tuple(
        sorted(
            {item.signal for item in observation.signal_evidence}, key=lambda x: x.value
        )
    )
    dispositions = {_DISPOSITIONS[item] for item in signals}
    if len(dispositions) > 1:
        raise LearningClassificationError(
            "learning.classification_ambiguous",
            "one observation names multiple durable authority owners",
        )
    disposition = (
        next(iter(dispositions))
        if dispositions
        else LearningDisposition.CANDIDATE_SPECIFIC_NO_CHANGE
    )
    return LearningClassification(
        observation.identity,
        DETERMINISTIC_LEARNING_CLASSIFIER_IDENTITY,
        disposition,
        signals,
    )


_PROPOSAL_SCOPES = {
    LearningDisposition.COMPONENT_BEHAVIOR: (
        LearningProposalScope.COMPONENT,
        "component_authority_identities",
    ),
    LearningDisposition.COMPONENT_INTERFACE: (
        LearningProposalScope.COMPONENT,
        "component_authority_identities",
    ),
    LearningDisposition.FLAVOR_VARIANCE: (
        LearningProposalScope.FLAVOR,
        "flavor_authority_identities",
    ),
    LearningDisposition.SKILL_TECHNIQUE: (
        LearningProposalScope.SKILL,
        "skill_authority_identities",
    ),
    LearningDisposition.WORKFLOW_HANDOFF: (
        LearningProposalScope.WORKFLOW,
        "workflow_authority_identities",
    ),
    LearningDisposition.ROUTING_ELIGIBILITY: (
        LearningProposalScope.ROUTING_POLICY,
        "routing_authority_identities",
    ),
    LearningDisposition.FRAMEWORK_DEFECT: (
        LearningProposalScope.FRAMEWORK,
        "framework_authority_identities",
    ),
    LearningDisposition.CANDIDATE_SPECIFIC_NO_CHANGE: (
        LearningProposalScope.NO_CHANGE,
        None,
    ),
}


def plan_learning_proposal(plan_input: LearningPlanInput) -> LearningProposal:
    """Read-only planning: return exactly one narrow, content-identified proposal."""

    if not isinstance(plan_input, LearningPlanInput):
        raise TypeError("plan_input must be a LearningPlanInput")
    classification = classify_learning_observation(plan_input.observation)
    scope, field = _PROPOSAL_SCOPES[classification.disposition]
    authority_identities = () if field is None else getattr(plan_input, field)
    if len(authority_identities) != (0 if field is None else 1):
        raise LearningClassificationError(
            "learning.proposal_owner_ambiguous",
            "proposal scope requires exactly one pinned authority owner",
        )
    semantic_delta = (
        ()
        if scope is LearningProposalScope.NO_CHANGE
        else plan_input.observation.fact_codes
    )
    return LearningProposal(
        plan_input.identity,
        plan_input.observation.identity,
        classification,
        scope,
        authority_identities,
        plan_input.test_suite_identities,
        plan_input.rebuild_component_identities,
        semantic_delta,
    )


__all__ = [
    "DETERMINISTIC_LEARNING_CLASSIFIER_IDENTITY",
    "LearningClassificationError",
    "classify_learning_observation",
    "plan_learning_proposal",
]
