"""Assemble exact Standard plan/cache/result membership and aggregate receipts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.executable_components import (
    ComponentExecutionPlan,
    SourceGenerationDisposition,
)
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardAggregateReceipt,
    StandardNodeCacheDecision,
    StandardNodeCacheOutcome,
    StandardPlannedLifecycleNode,
    StandardProjectLifecycleMembership,
)


class StandardLifecycleMembershipError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class StandardNodeResultEvidence(Protocol):
    component_revision: ContentIdentity
    source_generation: object
    source_cache_membership: object | None
    source_cache_publication_identity: ContentIdentity | None
    failure_evidence: object | None

    @property
    def identity(self) -> ContentIdentity: ...


def assemble_standard_lifecycle_membership(
    execution_plan: ComponentExecutionPlan,
    results: tuple[StandardNodeResultEvidence, ...],
    *,
    input_membership_identities: Mapping[str, ContentIdentity],
    forced_regeneration: frozenset[str],
    source_checkpoint_reuse: frozenset[str] = frozenset(),
) -> StandardProjectLifecycleMembership:
    """Prove plan nodes equal cache decisions equal canonical lifecycle results."""

    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("execution_plan must be a ComponentExecutionPlan")
    plans = {
        item.component_revision.uri: item for item in execution_plan.generation_plans
    }
    result_map = {item.component_revision.uri: item for item in results}
    if len(result_map) != len(results) or set(result_map) != set(plans):
        raise StandardLifecycleMembershipError(
            "standard_membership.result_set_mismatch",
            "lifecycle results must cover every and only planned Component",
        )
    if set(input_membership_identities) - set(plans):
        raise StandardLifecycleMembershipError(
            "standard_membership.cache_input_extra",
            "cache inputs contain a Component outside the exact plan",
        )
    if not forced_regeneration <= set(plans):
        raise StandardLifecycleMembershipError(
            "standard_membership.forced_node_extra",
            "forced regeneration contains a Component outside the exact plan",
        )
    if not source_checkpoint_reuse <= set(plans):
        raise StandardLifecycleMembershipError(
            "standard_membership.source_checkpoint_extra",
            "source checkpoint inputs contain a Component outside the exact plan",
        )
    planned = tuple(
        StandardPlannedLifecycleNode(
            plan.component_revision,
            plan.identity,
            plan.generation_key.identity,
        )
        for _uri, plan in sorted(plans.items())
    )
    decisions = []
    for uri, plan in sorted(plans.items()):
        result = result_map[uri]
        generation = result.source_generation
        if (
            getattr(generation, "generation_plan_identity", None) != plan.identity
            or getattr(generation, "generation_key_identity", None)
            != plan.generation_key.identity
        ):
            raise StandardLifecycleMembershipError(
                "standard_membership.result_plan_mismatch",
                "lifecycle result does not bind its exact planned node",
            )
        input_identity = input_membership_identities.get(uri)
        disposition = getattr(generation, "disposition", None)
        if uri in forced_regeneration:
            outcome = StandardNodeCacheOutcome.FORCED_REGENERATION
            input_identity = None
        elif uri in source_checkpoint_reuse:
            outcome = StandardNodeCacheOutcome.MISS
            input_identity = None
        elif disposition is SourceGenerationDisposition.REUSED or (
            disposition is SourceGenerationDisposition.CANCELLED
            and input_identity is not None
        ):
            if input_identity is None:
                raise StandardLifecycleMembershipError(
                    "standard_membership.hit_membership_missing",
                    "a reused lifecycle result requires its exact input membership",
                )
            outcome = StandardNodeCacheOutcome.HIT
        else:
            outcome = StandardNodeCacheOutcome.MISS
            input_identity = None
        accepted = result.source_cache_membership
        accepted_identity = None if accepted is None else accepted.identity
        failure = result.failure_evidence
        failure_identity = None if failure is None else failure.identity
        decisions.append(
            StandardNodeCacheDecision(
                plan.component_revision,
                plan.identity,
                plan.generation_key.identity,
                outcome,
                input_identity,
                result.identity,
                accepted_identity,
                result.source_cache_publication_identity,
                failure_identity,
            )
        )
    return StandardProjectLifecycleMembership(
        execution_plan.identity,
        planned,
        tuple(decisions),
    )


def create_standard_aggregate_receipt(
    execution_plan: ComponentExecutionPlan,
    membership: StandardProjectLifecycleMembership,
    admission_identity: ContentIdentity,
    context_prompt_journal_identities: tuple[ContentIdentity, ...],
    context_benchmark_record_identities: tuple[ContentIdentity, ...],
    context_cache_report_identity: ContentIdentity,
    root_integration_evidence_identity: ContentIdentity | None = None,
) -> StandardAggregateReceipt:
    """Create the only receipt shape admitted for one exact lifecycle membership."""

    if membership.execution_plan_identity != execution_plan.identity:
        raise StandardLifecycleMembershipError(
            "standard_membership.execution_plan_mismatch",
            "aggregate membership does not bind the exact execution plan",
        )
    return StandardAggregateReceipt(
        execution_plan.identity,
        membership.identity,
        membership.lifecycle_result_identities,
        admission_identity,
        context_prompt_journal_identities,
        context_benchmark_record_identities,
        context_cache_report_identity,
        root_integration_evidence_identity,
    )


def validate_standard_aggregate_receipt(
    receipt: StandardAggregateReceipt,
    execution_plan: ComponentExecutionPlan,
    membership: StandardProjectLifecycleMembership,
    admission_identity: ContentIdentity,
    context_prompt_journal_identities: tuple[ContentIdentity, ...],
    context_benchmark_record_identities: tuple[ContentIdentity, ...],
    context_cache_report_identity: ContentIdentity,
    root_integration_evidence_identity: ContentIdentity | None = None,
) -> None:
    """Reject any receipt whose plan, result set, membership, or admission differs."""

    if not isinstance(receipt, StandardAggregateReceipt):
        raise TypeError("receipt must be a StandardAggregateReceipt")
    expected = create_standard_aggregate_receipt(
        execution_plan,
        membership,
        admission_identity,
        context_prompt_journal_identities,
        context_benchmark_record_identities,
        context_cache_report_identity,
        root_integration_evidence_identity,
    )
    if receipt != expected:
        raise StandardLifecycleMembershipError(
            "standard_membership.receipt_mismatch",
            "aggregate receipt does not bind the exact lifecycle membership",
        )


__all__ = [
    "StandardLifecycleMembershipError",
    "StandardNodeResultEvidence",
    "assemble_standard_lifecycle_membership",
    "create_standard_aggregate_receipt",
    "validate_standard_aggregate_receipt",
]
