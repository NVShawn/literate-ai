"""Deterministic fail-closed evaluation of containment observations."""

from __future__ import annotations

from .contracts import (
    ContainmentControl,
    IsolationDecision,
    IsolationDecisionStatus,
    IsolationLevel,
    IsolationObservation,
    IsolationPolicy,
    IsolationRequest,
    mandatory_controls_for,
)


def _canonical_controls(
    *groups: tuple[ContainmentControl, ...],
) -> tuple[ContainmentControl, ...]:
    return tuple(
        sorted(
            {item for group in groups for item in group}, key=lambda item: item.value
        )
    )


def _decision(
    request: IsolationRequest,
    policy: IsolationPolicy,
    observation: IsolationObservation | None,
    *,
    status: IsolationDecisionStatus,
    required_level: IsolationLevel,
    required_controls: tuple[ContainmentControl, ...],
    missing_controls: tuple[ContainmentControl, ...] = (),
    reason_code: str,
) -> IsolationDecision:
    return IsolationDecision(
        request_identity=request.identity,
        policy_identity=policy.identity,
        observation_identity=None if observation is None else observation.identity,
        status=status,
        required_level=required_level,
        achieved_level=None if observation is None else observation.achieved_level,
        required_controls=required_controls,
        missing_controls=missing_controls,
        reason_code=reason_code,
    )


def evaluate_isolation_policy(
    request: IsolationRequest,
    policy: IsolationPolicy,
    observation: IsolationObservation | None,
) -> IsolationDecision:
    """Evaluate exact observed controls; absent or weaker evidence never passes."""

    if not isinstance(request, IsolationRequest):
        raise TypeError("request must be an IsolationRequest")
    if not isinstance(policy, IsolationPolicy):
        raise TypeError("policy must be an IsolationPolicy")
    if observation is not None and not isinstance(observation, IsolationObservation):
        raise TypeError("observation must be an IsolationObservation or None")

    rule = policy.rule_for(request.stage)
    if rule is None:
        required_controls = _canonical_controls(
            mandatory_controls_for(request.requested_level),
            request.required_controls,
        )
        return _decision(
            request,
            policy,
            None,
            status=IsolationDecisionStatus.UNSUPPORTED,
            required_level=request.requested_level,
            required_controls=required_controls,
            reason_code="containment.stage-unconfigured",
        )

    required_level = max(
        (request.requested_level, rule.minimum_level), key=lambda item: item.rank
    )
    required_controls = _canonical_controls(
        mandatory_controls_for(required_level),
        rule.required_controls,
        request.required_controls,
    )
    if required_level is IsolationLevel.HOST_YOLO:
        if not policy.allow_host_yolo:
            return _decision(
                request,
                policy,
                None,
                status=IsolationDecisionStatus.REJECTED,
                required_level=required_level,
                required_controls=required_controls,
                reason_code="containment.host-yolo-policy-denied",
            )
        if not request.host_yolo_acknowledged:
            return _decision(
                request,
                policy,
                None,
                status=IsolationDecisionStatus.REJECTED,
                required_level=required_level,
                required_controls=required_controls,
                reason_code="containment.host-yolo-acknowledgement-required",
            )
    if observation is None:
        return _decision(
            request,
            policy,
            observation,
            status=IsolationDecisionStatus.UNSUPPORTED,
            required_level=required_level,
            required_controls=required_controls,
            reason_code="containment.backend-unavailable",
        )
    if (
        observation.request_identity != request.identity
        or observation.policy_identity != policy.identity
    ):
        return _decision(
            request,
            policy,
            observation,
            status=IsolationDecisionStatus.REJECTED,
            required_level=required_level,
            required_controls=required_controls,
            reason_code="containment.observation-binding-mismatch",
        )
    if (
        observation.target_os != request.target_os
        or observation.target_architecture != request.target_architecture
    ):
        return _decision(
            request,
            policy,
            observation,
            status=IsolationDecisionStatus.REJECTED,
            required_level=required_level,
            required_controls=required_controls,
            reason_code="containment.observation-target-mismatch",
        )
    if not observation.complete:
        return _decision(
            request,
            policy,
            observation,
            status=IsolationDecisionStatus.REJECTED,
            required_level=required_level,
            required_controls=required_controls,
            reason_code="containment.enforcement-incomplete",
        )
    if not observation.achieved_level.satisfies(required_level):
        return _decision(
            request,
            policy,
            observation,
            status=IsolationDecisionStatus.UNSUPPORTED,
            required_level=required_level,
            required_controls=required_controls,
            reason_code="containment.isolation-level-unavailable",
        )
    missing_controls = tuple(
        item for item in required_controls if item not in observation.enforced_controls
    )
    if missing_controls:
        return _decision(
            request,
            policy,
            observation,
            status=IsolationDecisionStatus.UNSUPPORTED,
            required_level=required_level,
            required_controls=required_controls,
            missing_controls=missing_controls,
            reason_code="containment.controls-unavailable",
        )
    return _decision(
        request,
        policy,
        observation,
        status=IsolationDecisionStatus.REPORTED_SUFFICIENT,
        required_level=required_level,
        required_controls=required_controls,
        reason_code=(
            "containment.host-yolo-reported-only"
            if required_level is IsolationLevel.HOST_YOLO
            else "containment.reported-sufficient"
        ),
    )


__all__ = ["evaluate_isolation_policy"]
