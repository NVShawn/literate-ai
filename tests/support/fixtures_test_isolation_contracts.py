from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_isolation_contracts``."""



from literate_ai.security.isolation import (
    ContainmentControl,
    ContainmentStage,
    IsolationLevel,
    IsolationObservation,
    IsolationPolicy,
    IsolationRequest,
    StageIsolationRule,
    mandatory_controls_for,
)

_SUBJECT = "sha256:" + "1" * 64

_BACKEND = "sha256:" + "2" * 64

_EXECUTION = "sha256:" + "3" * 64

def _controls(*values: ContainmentControl) -> tuple[ContainmentControl, ...]:
    return tuple(sorted(set(values), key=lambda item: item.value))

def _policy(
    minimum: IsolationLevel = IsolationLevel.OS_SANDBOXED,
    *,
    allow_host_yolo: bool = False,
    controls: tuple[ContainmentControl, ...] | None = None,
) -> IsolationPolicy:
    return IsolationPolicy(
        policy_id="production-build",
        rules=(
            StageIsolationRule(
                ContainmentStage.BUILD,
                minimum,
                (
                    _controls(
                        ContainmentControl.NETWORK_DENIED,
                        ContainmentControl.READ_ONLY_INPUTS,
                        ContainmentControl.SEPARATE_OUTPUTS,
                    )
                    if controls is None
                    else controls
                ),
            ),
        ),
        allow_host_yolo=allow_host_yolo,
    )

def _request(
    level: IsolationLevel = IsolationLevel.OS_SANDBOXED,
    *,
    acknowledged: bool = False,
    stage: ContainmentStage = ContainmentStage.BUILD,
) -> IsolationRequest:
    return IsolationRequest(
        operation_id="build-component",
        stage=stage,
        subject_identity=_SUBJECT,
        target_os="linux",
        target_architecture="x86_64",
        requested_level=level,
        host_yolo_acknowledged=acknowledged,
    )

def _observation(
    level: IsolationLevel = IsolationLevel.OS_SANDBOXED,
    *,
    request: IsolationRequest | None = None,
    policy: IsolationPolicy | None = None,
    controls: tuple[ContainmentControl, ...] | None = None,
    complete: bool = True,
    target_os: str = "linux",
) -> IsolationObservation:
    request = _request() if request is None else request
    policy = _policy() if policy is None else policy
    return IsolationObservation(
        request_identity=request.identity,
        policy_identity=policy.identity,
        backend_id="fixture-sandbox",
        backend_version="fixture-1",
        backend_identity=_BACKEND,
        execution_identity=_EXECUTION,
        target_os=target_os,
        target_architecture="x86_64",
        achieved_level=level,
        enforced_controls=(
            _controls(
                *mandatory_controls_for(level),
                ContainmentControl.NETWORK_DENIED,
                ContainmentControl.READ_ONLY_INPUTS,
                ContainmentControl.SEPARATE_OUTPUTS,
            )
            if controls is None
            else controls
        ),
        complete=complete,
    )

