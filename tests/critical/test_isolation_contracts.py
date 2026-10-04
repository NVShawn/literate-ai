from __future__ import annotations

import unittest

from literate_ai.security.isolation import (
    ContainmentControl,
    ContainmentStage,
    IsolationDecisionStatus,
    IsolationLevel,
    IsolationObservation,
    IsolationPolicy,
    IsolationRequest,
    StageIsolationRule,
    evaluate_isolation_policy,
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


class IsolationContractTests(unittest.TestCase):
    def test_absent_backend_and_unconfigured_stage_fail_closed(self) -> None:
        unavailable = evaluate_isolation_policy(_request(), _policy(), None)
        unconfigured = evaluate_isolation_policy(
            _request(stage=ContainmentStage.APPLICATION_EXECUTION),
            _policy(),
            _observation(),
        )

        self.assertEqual(unavailable.status, IsolationDecisionStatus.UNSUPPORTED)
        self.assertEqual(unavailable.reason_code, "containment.backend-unavailable")
        self.assertIsNone(unavailable.observation_identity)
        self.assertEqual(unconfigured.status, IsolationDecisionStatus.UNSUPPORTED)
        self.assertEqual(unconfigured.reason_code, "containment.stage-unconfigured")
        unavailable.require_exact_recomputation(_request(), _policy(), None)

    def test_weaker_incomplete_or_wrong_target_observation_never_passes(self) -> None:
        cases = (
            (
                _observation(IsolationLevel.PROCESS_LIMITED),
                IsolationDecisionStatus.UNSUPPORTED,
                "containment.isolation-level-unavailable",
            ),
            (
                _observation(complete=False),
                IsolationDecisionStatus.REJECTED,
                "containment.enforcement-incomplete",
            ),
            (
                _observation(target_os="windows"),
                IsolationDecisionStatus.REJECTED,
                "containment.observation-target-mismatch",
            ),
        )
        for observation, status, code in cases:
            with self.subTest(code=code):
                decision = evaluate_isolation_policy(_request(), _policy(), observation)
                self.assertEqual(decision.status, status)
                self.assertEqual(decision.reason_code, code)
                self.assertFalse(decision.reported_sufficient)

    def test_host_yolo_requires_policy_and_request_acknowledgement(self) -> None:
        request = _request(IsolationLevel.HOST_YOLO)
        denied_policy = _policy(IsolationLevel.HOST_YOLO, controls=())
        allowed_policy = _policy(
            IsolationLevel.HOST_YOLO,
            allow_host_yolo=True,
            controls=(),
        )
        observation = _observation(
            IsolationLevel.HOST_YOLO,
            request=request,
            policy=denied_policy,
            controls=(),
        )
        denied = evaluate_isolation_policy(request, denied_policy, observation)
        allowed_observation = _observation(
            IsolationLevel.HOST_YOLO,
            request=request,
            policy=allowed_policy,
            controls=(),
        )
        unacknowledged = evaluate_isolation_policy(
            request,
            allowed_policy,
            allowed_observation,
        )
        acknowledged_request = _request(IsolationLevel.HOST_YOLO, acknowledged=True)
        acknowledged_observation = _observation(
            IsolationLevel.HOST_YOLO,
            request=acknowledged_request,
            policy=allowed_policy,
            controls=(),
        )
        accepted = evaluate_isolation_policy(
            acknowledged_request,
            allowed_policy,
            acknowledged_observation,
        )

        self.assertEqual(denied.reason_code, "containment.host-yolo-policy-denied")
        self.assertEqual(
            unacknowledged.reason_code,
            "containment.host-yolo-acknowledgement-required",
        )
        self.assertTrue(accepted.reported_sufficient)
        self.assertEqual(accepted.reason_code, "containment.host-yolo-reported-only")


if __name__ == "__main__":
    unittest.main()
