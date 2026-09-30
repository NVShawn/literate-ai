from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.security.isolation import (
    ContainmentControl,
    ContainmentStage,
    IsolationDecision,
    IsolationDecisionStatus,
    IsolationEvidenceAuthentication,
    IsolationLevel,
    IsolationObservation,
    IsolationPolicy,
    IsolationPolicyError,
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
    def test_isolation_levels_are_strictly_ordered(self) -> None:
        levels = tuple(IsolationLevel)
        self.assertEqual(
            levels,
            (
                IsolationLevel.HOST_YOLO,
                IsolationLevel.PROCESS_LIMITED,
                IsolationLevel.OS_SANDBOXED,
                IsolationLevel.VM_ISOLATED,
            ),
        )
        for achieved in levels:
            for required in levels:
                self.assertEqual(
                    achieved.satisfies(required), achieved.rank >= required.rank
                )

    def test_stronger_observation_satisfies_weaker_requirement(self) -> None:
        decision = evaluate_isolation_policy(
            _request(), _policy(), _observation(IsolationLevel.VM_ISOLATED)
        )

        self.assertTrue(decision.reported_sufficient)
        self.assertEqual(decision.status, IsolationDecisionStatus.REPORTED_SUFFICIENT)
        self.assertEqual(decision.required_level, IsolationLevel.OS_SANDBOXED)
        self.assertEqual(decision.achieved_level, IsolationLevel.VM_ISOLATED)
        self.assertEqual(
            decision.authentication,
            IsolationEvidenceAuthentication.UNAUTHENTICATED_LOCAL,
        )
        decision.require_exact_recomputation(
            _request(), _policy(), _observation(IsolationLevel.VM_ISOLATED)
        )

    def test_request_can_raise_but_never_lower_policy_minimum(self) -> None:
        raised_request = _request(IsolationLevel.VM_ISOLATED)
        raised_policy = _policy(IsolationLevel.PROCESS_LIMITED)
        raised = evaluate_isolation_policy(
            raised_request,
            raised_policy,
            _observation(
                IsolationLevel.OS_SANDBOXED,
                request=raised_request,
                policy=raised_policy,
            ),
        )
        lower_request = _request(IsolationLevel.PROCESS_LIMITED)
        stronger_policy = _policy(IsolationLevel.OS_SANDBOXED)
        policy_wins = evaluate_isolation_policy(
            lower_request,
            stronger_policy,
            _observation(
                IsolationLevel.PROCESS_LIMITED,
                request=lower_request,
                policy=stronger_policy,
            ),
        )

        self.assertEqual(raised.required_level, IsolationLevel.VM_ISOLATED)
        self.assertEqual(raised.status, IsolationDecisionStatus.UNSUPPORTED)
        self.assertEqual(policy_wins.required_level, IsolationLevel.OS_SANDBOXED)
        self.assertEqual(policy_wins.status, IsolationDecisionStatus.UNSUPPORTED)

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

    def test_missing_control_is_exactly_reported(self) -> None:
        observation = _observation(
            controls=_controls(
                *mandatory_controls_for(IsolationLevel.OS_SANDBOXED),
            )
        )
        decision = evaluate_isolation_policy(_request(), _policy(), observation)

        self.assertEqual(decision.status, IsolationDecisionStatus.UNSUPPORTED)
        self.assertEqual(decision.reason_code, "containment.controls-unavailable")
        self.assertEqual(
            decision.missing_controls, (ContainmentControl.NETWORK_DENIED,)
        )

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

    def test_host_yolo_observation_cannot_claim_controls(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot claim containment controls"):
            _observation(IsolationLevel.HOST_YOLO)

    def test_observation_must_bind_the_exact_request_and_policy(self) -> None:
        request = _request()
        policy = _policy()
        observation = _observation()
        changed_request = replace(request, operation_id="another-build")
        changed_policy = replace(policy, policy_id="another-policy")

        for actual_request, actual_policy in (
            (changed_request, policy),
            (request, changed_policy),
        ):
            with self.subTest(
                request=actual_request.operation_id, policy=actual_policy.policy_id
            ):
                decision = evaluate_isolation_policy(
                    actual_request, actual_policy, observation
                )
                self.assertEqual(decision.status, IsolationDecisionStatus.REJECTED)
                self.assertEqual(
                    decision.reason_code,
                    "containment.observation-binding-mismatch",
                )

    def test_contracts_round_trip_with_stable_identities(self) -> None:
        policy = _policy()
        request = _request()
        observation = _observation()
        decision = evaluate_isolation_policy(request, policy, observation)

        values = (
            (policy, IsolationPolicy),
            (request, IsolationRequest),
            (observation, IsolationObservation),
            (decision, IsolationDecision),
        )
        for original, contract in values:
            with self.subTest(contract=contract.__name__):
                decoded = contract.from_dict(original.to_dict())
                self.assertEqual(decoded, original)
                self.assertEqual(decoded.identity, original.identity)

    def test_unknown_fields_versions_and_noncanonical_controls_are_rejected(
        self,
    ) -> None:
        unknown = _policy().to_dict()
        unknown["unexpected"] = True
        future = _request().to_dict()
        future["schema"] = "literate-ai/isolation-request@2"

        with self.assertRaisesRegex(ValueError, "unknown fields"):
            IsolationPolicy.from_dict(unknown)
        with self.assertRaisesRegex(ValueError, "must be.*isolation-request@1"):
            IsolationRequest.from_dict(future)
        with self.assertRaisesRegex(ValueError, "canonical lexical order"):
            StageIsolationRule(
                ContainmentStage.BUILD,
                IsolationLevel.OS_SANDBOXED,
                (
                    ContainmentControl.SEPARATE_OUTPUTS,
                    ContainmentControl.READ_ONLY_INPUTS,
                ),
            )

    def test_reported_sufficient_decision_rejects_weak_reported_facts(self) -> None:
        request = _request()
        policy = _policy()
        observation = _observation(IsolationLevel.PROCESS_LIMITED)
        decision = evaluate_isolation_policy(request, policy, observation).to_dict()
        decision["status"] = "reported-sufficient"
        decision["reason_code"] = "containment.reported-sufficient"

        with self.assertRaisesRegex(ValueError, "sufficient reported facts"):
            IsolationDecision.from_dict(decision)

    def test_complete_observation_must_report_intrinsic_level_controls(self) -> None:
        with self.assertRaisesRegex(ValueError, "intrinsic"):
            _observation(
                IsolationLevel.OS_SANDBOXED,
                controls=_controls(ContainmentControl.NETWORK_DENIED),
            )

    def test_status_reason_pairs_are_closed(self) -> None:
        decision = evaluate_isolation_policy(_request(), _policy(), None).to_dict()
        decision["status"] = "rejected"

        with self.assertRaisesRegex(ValueError, "inconsistent with status"):
            IsolationDecision.from_dict(decision)

    def test_decision_must_be_recomputed_against_exact_input_closure(self) -> None:
        request = _request()
        policy = _policy()
        observation = _observation()
        decision = evaluate_isolation_policy(request, policy, observation)
        substituted_observation = replace(
            observation,
            execution_identity="sha256:" + "4" * 64,
        )

        with self.assertRaisesRegex(
            IsolationPolicyError, "containment.decision-mismatch"
        ):
            decision.require_exact_recomputation(
                request, policy, substituted_observation
            )


if __name__ == "__main__":
    unittest.main()
