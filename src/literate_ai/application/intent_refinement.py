"""Deterministic intent refinement before lock and plan (ADR 0013).

This service does not invoke a coding CLI. It classifies a mission request into
concerns, proposes the narrowest Component graph it can defend, and fails closed
when blocking questions remain unanswered. Acceptance records a receipt; it
never generates source.
"""

from __future__ import annotations

import re

from literate_ai.contracts.intent_refinement import (
    ConcernKind,
    ConcernRecord,
    DesignAcceptanceReceipt,
    DesignAcceptRequest,
    DesignDraft,
    DesignStatus,
    IntentRefinementRequest,
    ProposedComponent,
    SufficiencyStatus,
    UnresolvedInput,
    UnresolvedKind,
)

_TOKEN = re.compile(r"[a-z0-9]+")


class IntentRefinementError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class IntentRefinementService:
    def refine(self, request: IntentRefinementRequest) -> DesignDraft:
        mission = request.mission.strip()
        if not mission:
            raise IntentRefinementError(
                "intent_refinement.mission_empty",
                "a mission request must contain observable product intent",
            )
        tokens = set(_TOKEN.findall(mission.casefold()))
        mixed = _is_mixed_depth(tokens)
        if mixed:
            return _mixed_depth_draft(request)
        return _single_component_draft(request)

    def explain(
        self,
        draft: DesignDraft,
        *,
        component: str | None = None,
        concern: str | None = None,
    ) -> dict[str, object]:
        components = draft.components
        concerns = draft.concerns
        unresolved = draft.unresolved
        if component is not None:
            components = tuple(
                item for item in components if item.component_id == component
            )
            if not components:
                raise IntentRefinementError(
                    "intent_refinement.component_unknown",
                    f"design draft has no Component {component!r}",
                )
            owned = {item for record in components for item in record.concern_ids}
            concerns = tuple(item for item in concerns if item.concern_id in owned)
            unresolved = tuple(item for item in unresolved if item.concern_id in owned)
        if concern is not None:
            concerns = tuple(item for item in concerns if item.concern_id == concern)
            if not concerns:
                raise IntentRefinementError(
                    "intent_refinement.concern_unknown",
                    f"design draft has no concern {concern!r}",
                )
            unresolved = tuple(
                item for item in unresolved if item.concern_id == concern
            )
        blocking = [
            item.to_dict()
            for item in unresolved
            if item.kind is UnresolvedKind.BLOCKING
        ]
        return {
            "schema": "literate-ai/design-explain@1",
            "draft_identity": draft.identity.uri,
            "status": draft.status.value,
            "components": [item.to_dict() for item in components],
            "concerns": [item.to_dict() for item in concerns],
            "blocking": blocking,
            "unresolved": [item.to_dict() for item in unresolved],
        }

    def accept(
        self, draft: DesignDraft, request: DesignAcceptRequest
    ) -> DesignAcceptanceReceipt:
        if request.draft_identity != draft.identity.uri:
            raise IntentRefinementError(
                "intent_refinement.draft_identity_mismatch",
                "accept decisions must name the exact draft identity just reviewed",
            )
        blocking = draft.blocking_questions()
        answers = {item.question_id: item.answer for item in request.decisions}
        missing = [
            item.question_id for item in blocking if item.question_id not in answers
        ]
        if missing:
            raise IntentRefinementError(
                "intent_refinement.blocking_unanswered",
                "acceptance cannot proceed while blocking questions remain: "
                + ", ".join(missing),
            )
        known = {item.question_id for item in draft.unresolved}
        extra = [
            item.question_id
            for item in request.decisions
            if item.question_id not in known
        ]
        if extra:
            raise IntentRefinementError(
                "intent_refinement.extra_decision",
                "accept decisions named questions the draft does not ask: "
                + ", ".join(extra),
            )
        for item in draft.unresolved:
            if item.kind is UnresolvedKind.TARGET_BOUND:
                continue
        return DesignAcceptanceReceipt(
            draft_identity=draft.identity.uri,
            request_identity=draft.request_identity,
            accepted_component_ids=tuple(
                item.component_id
                for item in sorted(draft.components, key=lambda item: item.component_id)
            ),
            decision_ids=tuple(
                item.question_id
                for item in sorted(request.decisions, key=lambda item: item.question_id)
            ),
        )


def _is_mixed_depth(tokens: set[str]) -> bool:
    compute = bool(tokens & {"pi", "gpu", "cuda", "cpu", "numerical", "precision"})
    surface = bool(tokens & {"http", "api", "frontend", "web", "ui", "stream"})
    host = bool(tokens & {"gpu", "cuda", "cpu", "server", "servers"})
    return sum((compute, surface, host)) >= 2


def _single_component_draft(request: IntentRefinementRequest) -> DesignDraft:
    concern = ConcernRecord(
        concern_id="portable-behavior",
        kind=ConcernKind.APPLICATION_SURFACE,
        summary="One portable application surface matches the mission request.",
        proposed_authority="component.md literate-markdown behavioral root",
        sufficiency=SufficiencyStatus.SUFFICIENT,
    )
    component = ProposedComponent(
        component_id="application",
        role="portable-application",
        boundary_reason="The request describes one observable product surface.",
        concern_ids=("portable-behavior",),
    )
    return DesignDraft(
        request_identity=request.identity.uri,
        status=DesignStatus.COMPLETE,
        mission=request.mission,
        components=(component,),
        concerns=(concern,),
        unresolved=(),
        non_goals=("Do not invent extra Components for unspecified surfaces.",),
    )


def _mixed_depth_draft(request: IntentRefinementRequest) -> DesignDraft:
    concerns = (
        ConcernRecord(
            concern_id="compute-kernel",
            kind=ConcernKind.COMPUTATION,
            summary="Numerical computation needs an independent correctness contract.",
            proposed_authority="Component specification with measurable numeric bounds",
            sufficiency=SufficiencyStatus.NEEDS_AUTHORITY,
        ),
        ConcernRecord(
            concern_id="progress-protocol",
            kind=ConcernKind.PROTOCOL,
            summary="Streamed progress is a public protocol, not a UI detail.",
            proposed_authority="interface or library Component",
            sufficiency=SufficiencyStatus.NEEDS_AUTHORITY,
        ),
        ConcernRecord(
            concern_id="web-frontend",
            kind=ConcernKind.APPLICATION_SURFACE,
            summary="Presentation can stay a shallow UI Component.",
            proposed_authority="web-application Component",
            sufficiency=SufficiencyStatus.SUFFICIENT,
        ),
        ConcernRecord(
            concern_id="host-selection",
            kind=ConcernKind.TARGET,
            summary="CPU and GPU placement is a Flavor/target choice.",
            proposed_authority="target profile and accelerator Flavors",
            sufficiency=SufficiencyStatus.TARGET_UNRESOLVED,
        ),
        ConcernRecord(
            concern_id="trust-boundary",
            kind=ConcernKind.TRUST,
            summary="Authentication and tenant isolation are unspecified.",
            proposed_authority="blocking product decision before generation",
            sufficiency=SufficiencyStatus.NEEDS_AUTHORITY,
        ),
    )
    components = (
        ProposedComponent(
            component_id="pi-kernel",
            role="computation",
            boundary_reason="Numeric correctness is independently verifiable.",
            concern_ids=("compute-kernel",),
        ),
        ProposedComponent(
            component_id="pi-progress",
            role="protocol",
            boundary_reason="Streaming and reconnect semantics are a public contract.",
            concern_ids=("progress-protocol",),
        ),
        ProposedComponent(
            component_id="pi-frontend",
            role="web-application",
            boundary_reason="Display does not own computation or measurement.",
            concern_ids=("web-frontend",),
        ),
        ProposedComponent(
            component_id="pi-service",
            role="persistent-service",
            boundary_reason=(
                "API, jobs, and host accounting compose the other Components."
            ),
            concern_ids=("host-selection", "trust-boundary"),
        ),
    )
    unresolved = (
        UnresolvedInput(
            question_id="numeric-error-bound",
            kind=UnresolvedKind.BLOCKING,
            concern_id="compute-kernel",
            prompt="What numeric error guarantee must the pi computation satisfy?",
            why_observable=(
                "Without a bound, generated tests cannot decide correctness."
            ),
        ),
        UnresolvedInput(
            question_id="partial-result-meaning",
            kind=UnresolvedKind.BLOCKING,
            concern_id="progress-protocol",
            prompt=(
                "What does a streamed partial result mean if the job is interrupted?"
            ),
            why_observable="Clients cannot interpret progress without that contract.",
        ),
        UnresolvedInput(
            question_id="cpu-gpu-accounting",
            kind=UnresolvedKind.BLOCKING,
            concern_id="host-selection",
            prompt="What clocks, scopes, and aggregation define CPU/GPU time?",
            why_observable=(
                "Resource names are meaningless until their semantics exist."
            ),
        ),
        UnresolvedInput(
            question_id="authentication-boundary",
            kind=UnresolvedKind.BLOCKING,
            concern_id="trust-boundary",
            prompt="Who may call the service, and what is the trust boundary?",
            why_observable=(
                "Security questions default to blocking when the request is silent."
            ),
        ),
        UnresolvedInput(
            question_id="accelerator-flavor",
            kind=UnresolvedKind.TARGET_BOUND,
            concern_id="host-selection",
            prompt=(
                "Which host OS and accelerator Flavors should lock the compute workers?"
            ),
            why_observable=(
                "Host selection must not leak into portable numeric behavior."
            ),
        ),
    )
    return DesignDraft(
        request_identity=request.identity.uri,
        status=DesignStatus.NEEDS_DECISIONS,
        mission=request.mission,
        components=components,
        concerns=concerns,
        unresolved=unresolved,
        non_goals=(
            "Do not copy host CUDA APIs into the portable computation specification.",
            "Do not flatten independently executable surfaces into one Component.",
        ),
    )


__all__ = ["IntentRefinementError", "IntentRefinementService"]
