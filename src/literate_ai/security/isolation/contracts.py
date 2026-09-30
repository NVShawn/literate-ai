"""Immutable provider-neutral containment contracts.

This module defines policy contracts only.  It deliberately does not register or
instantiate an operating-system or virtual-machine isolation backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final

from literate_ai.contracts._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    fields,
    optional_string,
    string_tuple,
    string_value,
    unique,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

ISOLATION_POLICY_SCHEMA: Final = "literate-ai/isolation-policy@1"
ISOLATION_REQUEST_SCHEMA: Final = "literate-ai/isolation-request@1"
ISOLATION_OBSERVATION_SCHEMA: Final = "literate-ai/isolation-observation@1"
ISOLATION_DECISION_SCHEMA: Final = "literate-ai/isolation-decision@1"

_PORTABLE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


class IsolationPolicyError(RuntimeError):
    """A requested operation lacks sufficient containment evidence."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IsolationLevel(StrEnum):
    """Ordered containment guarantees, from no boundary to a VM boundary."""

    HOST_YOLO = "host-yolo"
    PROCESS_LIMITED = "process-limited"
    OS_SANDBOXED = "os-sandboxed"
    VM_ISOLATED = "vm-isolated"

    @property
    def rank(self) -> int:
        return _ISOLATION_RANK[self]

    def satisfies(self, required: IsolationLevel) -> bool:
        if not isinstance(required, IsolationLevel):
            raise TypeError("required must be an IsolationLevel")
        return self.rank >= required.rank


_ISOLATION_RANK: Final = {
    IsolationLevel.HOST_YOLO: 0,
    IsolationLevel.PROCESS_LIMITED: 1,
    IsolationLevel.OS_SANDBOXED: 2,
    IsolationLevel.VM_ISOLATED: 3,
}


class ContainmentStage(StrEnum):
    SOURCE_ACQUISITION = "source-acquisition"
    SOURCE_GENERATION = "source-generation"
    SOURCE_OBSERVATION = "source-observation"
    DEPENDENCY_RESOLUTION = "dependency-resolution"
    BUILD = "build"
    GENERATED_TEST = "generated-test"
    APPLICATION_EXECUTION = "application-execution"


class ContainmentControl(StrEnum):
    """Independently observable controls which a policy may require."""

    READ_ONLY_INPUTS = "read-only-inputs"
    SEPARATE_OUTPUTS = "separate-outputs"
    MINIMAL_ENVIRONMENT = "minimal-environment"
    NO_CREDENTIALS = "no-credentials"
    NO_DEVICES = "no-devices"
    NETWORK_DENIED = "network-denied"
    NETWORK_EGRESS_RESTRICTED = "network-egress-restricted"
    RESOURCE_LIMITS = "resource-limits"
    WALL_TIME_LIMIT = "wall-time-limit"
    OUTPUT_LIMIT = "output-limit"
    PROCESS_TREE_TERMINATION = "process-tree-termination"


class IsolationDecisionStatus(StrEnum):
    REPORTED_SUFFICIENT = "reported-sufficient"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"


class IsolationEvidenceAuthentication(StrEnum):
    """Authentication state carried by this local policy-decision record."""

    UNAUTHENTICATED_LOCAL = "unauthenticated-local"


_MANDATORY_LEVEL_CONTROLS: Final = {
    IsolationLevel.HOST_YOLO: (),
    IsolationLevel.PROCESS_LIMITED: (
        ContainmentControl.OUTPUT_LIMIT,
        ContainmentControl.PROCESS_TREE_TERMINATION,
        ContainmentControl.RESOURCE_LIMITS,
        ContainmentControl.WALL_TIME_LIMIT,
    ),
    IsolationLevel.OS_SANDBOXED: (
        ContainmentControl.MINIMAL_ENVIRONMENT,
        ContainmentControl.NO_CREDENTIALS,
        ContainmentControl.NO_DEVICES,
        ContainmentControl.OUTPUT_LIMIT,
        ContainmentControl.PROCESS_TREE_TERMINATION,
        ContainmentControl.READ_ONLY_INPUTS,
        ContainmentControl.RESOURCE_LIMITS,
        ContainmentControl.SEPARATE_OUTPUTS,
        ContainmentControl.WALL_TIME_LIMIT,
    ),
    IsolationLevel.VM_ISOLATED: (
        ContainmentControl.MINIMAL_ENVIRONMENT,
        ContainmentControl.NO_CREDENTIALS,
        ContainmentControl.NO_DEVICES,
        ContainmentControl.OUTPUT_LIMIT,
        ContainmentControl.PROCESS_TREE_TERMINATION,
        ContainmentControl.READ_ONLY_INPUTS,
        ContainmentControl.RESOURCE_LIMITS,
        ContainmentControl.SEPARATE_OUTPUTS,
        ContainmentControl.WALL_TIME_LIMIT,
    ),
}


def mandatory_controls_for(
    level: IsolationLevel,
) -> tuple[ContainmentControl, ...]:
    """Return controls intrinsic to a claimed isolation level."""

    if not isinstance(level, IsolationLevel):
        raise TypeError("level must be an IsolationLevel")
    return _MANDATORY_LEVEL_CONTROLS[level]


def _portable_name(value: str, path: str) -> str:
    result = string_value(value, path, max_length=128)
    if not _PORTABLE_NAME.fullmatch(result):
        fail(path, "must be a portable lower-case identifier")
    return result


def _identity(value: str, path: str) -> str:
    result = string_value(value, path)
    try:
        parsed = ContentIdentity.parse_uri(result)
    except ValueError as exc:
        fail(path, f"must be a lowercase sha256 identity ({exc})")
    return parsed.uri


def _controls(
    values: tuple[ContainmentControl, ...], path: str
) -> tuple[ContainmentControl, ...]:
    if not isinstance(values, tuple):
        fail(path, "must be a tuple")
    if any(not isinstance(item, ContainmentControl) for item in values):
        fail(path, "must contain ContainmentControl values")
    unique(values, path, "controls")
    if tuple(sorted(values, key=lambda item: item.value)) != values:
        fail(path, "must be in canonical lexical order")
    return values


def _parse_controls(value: object, path: str) -> tuple[ContainmentControl, ...]:
    parsed = tuple(
        enum_value(ContainmentControl, item, f"{path}[{index}]")
        for index, item in enumerate(string_tuple(value, path))
    )
    return _controls(parsed, path)


@dataclass(frozen=True, slots=True)
class StageIsolationRule:
    stage: ContainmentStage
    minimum_level: IsolationLevel
    required_controls: tuple[ContainmentControl, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ContainmentStage):
            raise TypeError("stage must be a ContainmentStage")
        if not isinstance(self.minimum_level, IsolationLevel):
            raise TypeError("minimum_level must be an IsolationLevel")
        _controls(self.required_controls, "StageIsolationRule.required_controls")

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "minimum_level": self.minimum_level.value,
            "required_controls": [item.value for item in self.required_controls],
        }

    @classmethod
    def from_dict(
        cls, value: object, *, path: str = "StageIsolationRule"
    ) -> StageIsolationRule:
        data = fields(
            value,
            path=path,
            required=frozenset({"stage", "minimum_level", "required_controls"}),
        )
        return cls(
            stage=enum_value(ContainmentStage, data["stage"], f"{path}.stage"),
            minimum_level=enum_value(
                IsolationLevel, data["minimum_level"], f"{path}.minimum_level"
            ),
            required_controls=_parse_controls(
                data["required_controls"], f"{path}.required_controls"
            ),
        )


@dataclass(frozen=True, slots=True)
class IsolationPolicy:
    """Stage-specific minimum guarantees; an omitted stage is unsupported."""

    policy_id: str
    rules: tuple[StageIsolationRule, ...]
    allow_host_yolo: bool = False

    SCHEMA: ClassVar[str] = ISOLATION_POLICY_SCHEMA

    def __post_init__(self) -> None:
        _portable_name(self.policy_id, "IsolationPolicy.policy_id")
        if not isinstance(self.rules, tuple) or not self.rules:
            fail("IsolationPolicy.rules", "must be a non-empty tuple")
        if any(not isinstance(item, StageIsolationRule) for item in self.rules):
            fail("IsolationPolicy.rules", "must contain StageIsolationRule values")
        stages = tuple(item.stage for item in self.rules)
        unique(stages, "IsolationPolicy.rules", "stages")
        if tuple(sorted(stages, key=lambda item: item.value)) != stages:
            fail("IsolationPolicy.rules", "must be in canonical stage order")
        if not isinstance(self.allow_host_yolo, bool):
            fail("IsolationPolicy.allow_host_yolo", "must be a boolean")

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def rule_for(self, stage: ContainmentStage) -> StageIsolationRule | None:
        if not isinstance(stage, ContainmentStage):
            raise TypeError("stage must be a ContainmentStage")
        return next((item for item in self.rules if item.stage is stage), None)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "policy_id": self.policy_id,
            "rules": [item.to_dict() for item in self.rules],
            "allow_host_yolo": self.allow_host_yolo,
        }

    @classmethod
    def from_dict(cls, value: object) -> IsolationPolicy:
        data = contract_fields(
            value,
            path="IsolationPolicy",
            schema_uri=cls.SCHEMA,
            required=frozenset({"policy_id", "rules", "allow_host_yolo"}),
        )
        raw_rules = data["rules"]
        if isinstance(raw_rules, (str, bytes, bytearray)) or not isinstance(
            raw_rules, (list, tuple)
        ):
            fail("IsolationPolicy.rules", "must be an array")
        return cls(
            policy_id=_portable_name(data["policy_id"], "IsolationPolicy.policy_id"),
            rules=tuple(
                StageIsolationRule.from_dict(
                    item, path=f"IsolationPolicy.rules[{index}]"
                )
                for index, item in enumerate(raw_rules)
            ),
            allow_host_yolo=bool_value(
                data["allow_host_yolo"], "IsolationPolicy.allow_host_yolo"
            ),
        )


@dataclass(frozen=True, slots=True)
class IsolationRequest:
    operation_id: str
    stage: ContainmentStage
    subject_identity: str
    target_os: str
    target_architecture: str
    requested_level: IsolationLevel
    required_controls: tuple[ContainmentControl, ...] = ()
    host_yolo_acknowledged: bool = False

    SCHEMA: ClassVar[str] = ISOLATION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        _portable_name(self.operation_id, "IsolationRequest.operation_id")
        if not isinstance(self.stage, ContainmentStage):
            raise TypeError("stage must be a ContainmentStage")
        _identity(self.subject_identity, "IsolationRequest.subject_identity")
        _portable_name(self.target_os, "IsolationRequest.target_os")
        _portable_name(self.target_architecture, "IsolationRequest.target_architecture")
        if not isinstance(self.requested_level, IsolationLevel):
            raise TypeError("requested_level must be an IsolationLevel")
        _controls(self.required_controls, "IsolationRequest.required_controls")
        if not isinstance(self.host_yolo_acknowledged, bool):
            fail("IsolationRequest.host_yolo_acknowledged", "must be a boolean")

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "operation_id": self.operation_id,
            "stage": self.stage.value,
            "subject_identity": self.subject_identity,
            "target_os": self.target_os,
            "target_architecture": self.target_architecture,
            "requested_level": self.requested_level.value,
            "required_controls": [item.value for item in self.required_controls],
            "host_yolo_acknowledged": self.host_yolo_acknowledged,
        }

    @classmethod
    def from_dict(cls, value: object) -> IsolationRequest:
        data = contract_fields(
            value,
            path="IsolationRequest",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "operation_id",
                    "stage",
                    "subject_identity",
                    "target_os",
                    "target_architecture",
                    "requested_level",
                    "required_controls",
                    "host_yolo_acknowledged",
                }
            ),
        )
        return cls(
            operation_id=_portable_name(
                data["operation_id"], "IsolationRequest.operation_id"
            ),
            stage=enum_value(ContainmentStage, data["stage"], "IsolationRequest.stage"),
            subject_identity=_identity(
                data["subject_identity"], "IsolationRequest.subject_identity"
            ),
            target_os=_portable_name(data["target_os"], "IsolationRequest.target_os"),
            target_architecture=_portable_name(
                data["target_architecture"],
                "IsolationRequest.target_architecture",
            ),
            requested_level=enum_value(
                IsolationLevel,
                data["requested_level"],
                "IsolationRequest.requested_level",
            ),
            required_controls=_parse_controls(
                data["required_controls"], "IsolationRequest.required_controls"
            ),
            host_yolo_acknowledged=bool_value(
                data["host_yolo_acknowledged"],
                "IsolationRequest.host_yolo_acknowledged",
            ),
        )


@dataclass(frozen=True, slots=True)
class IsolationObservation:
    """Caller-supplied enforcement facts; not an authenticated attestation."""

    request_identity: str
    policy_identity: str
    backend_id: str
    backend_version: str
    backend_identity: str
    execution_identity: str
    target_os: str
    target_architecture: str
    achieved_level: IsolationLevel
    enforced_controls: tuple[ContainmentControl, ...]
    complete: bool

    SCHEMA: ClassVar[str] = ISOLATION_OBSERVATION_SCHEMA

    def __post_init__(self) -> None:
        _identity(self.request_identity, "IsolationObservation.request_identity")
        _identity(self.policy_identity, "IsolationObservation.policy_identity")
        _portable_name(self.backend_id, "IsolationObservation.backend_id")
        string_value(
            self.backend_version, "IsolationObservation.backend_version", max_length=256
        )
        _identity(self.backend_identity, "IsolationObservation.backend_identity")
        _identity(self.execution_identity, "IsolationObservation.execution_identity")
        _portable_name(self.target_os, "IsolationObservation.target_os")
        _portable_name(
            self.target_architecture, "IsolationObservation.target_architecture"
        )
        if not isinstance(self.achieved_level, IsolationLevel):
            raise TypeError("achieved_level must be an IsolationLevel")
        _controls(self.enforced_controls, "IsolationObservation.enforced_controls")
        if self.achieved_level is IsolationLevel.HOST_YOLO and self.enforced_controls:
            fail(
                "IsolationObservation.enforced_controls",
                "host-yolo cannot claim containment controls",
            )
        if not isinstance(self.complete, bool):
            fail("IsolationObservation.complete", "must be a boolean")
        if self.complete:
            missing = tuple(
                item
                for item in mandatory_controls_for(self.achieved_level)
                if item not in self.enforced_controls
            )
            if missing:
                fail(
                    "IsolationObservation.enforced_controls",
                    "a complete observation must report every control intrinsic "
                    "to its achieved isolation level",
                )

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity,
            "policy_identity": self.policy_identity,
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "backend_identity": self.backend_identity,
            "execution_identity": self.execution_identity,
            "target_os": self.target_os,
            "target_architecture": self.target_architecture,
            "achieved_level": self.achieved_level.value,
            "enforced_controls": [item.value for item in self.enforced_controls],
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, value: object) -> IsolationObservation:
        data = contract_fields(
            value,
            path="IsolationObservation",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "request_identity",
                    "policy_identity",
                    "backend_id",
                    "backend_version",
                    "backend_identity",
                    "execution_identity",
                    "target_os",
                    "target_architecture",
                    "achieved_level",
                    "enforced_controls",
                    "complete",
                }
            ),
        )
        return cls(
            request_identity=_identity(
                data["request_identity"], "IsolationObservation.request_identity"
            ),
            policy_identity=_identity(
                data["policy_identity"], "IsolationObservation.policy_identity"
            ),
            backend_id=_portable_name(
                data["backend_id"], "IsolationObservation.backend_id"
            ),
            backend_version=string_value(
                data["backend_version"],
                "IsolationObservation.backend_version",
                max_length=256,
            ),
            backend_identity=_identity(
                data["backend_identity"], "IsolationObservation.backend_identity"
            ),
            execution_identity=_identity(
                data["execution_identity"], "IsolationObservation.execution_identity"
            ),
            target_os=_portable_name(
                data["target_os"], "IsolationObservation.target_os"
            ),
            target_architecture=_portable_name(
                data["target_architecture"],
                "IsolationObservation.target_architecture",
            ),
            achieved_level=enum_value(
                IsolationLevel,
                data["achieved_level"],
                "IsolationObservation.achieved_level",
            ),
            enforced_controls=_parse_controls(
                data["enforced_controls"],
                "IsolationObservation.enforced_controls",
            ),
            complete=bool_value(data["complete"], "IsolationObservation.complete"),
        )


@dataclass(frozen=True, slots=True)
class IsolationDecision:
    """Non-authorizing report of caller-supplied local enforcement facts."""

    request_identity: str
    policy_identity: str
    observation_identity: str | None
    status: IsolationDecisionStatus
    required_level: IsolationLevel
    achieved_level: IsolationLevel | None
    required_controls: tuple[ContainmentControl, ...]
    missing_controls: tuple[ContainmentControl, ...]
    reason_code: str
    authentication: IsolationEvidenceAuthentication = (
        IsolationEvidenceAuthentication.UNAUTHENTICATED_LOCAL
    )

    SCHEMA: ClassVar[str] = ISOLATION_DECISION_SCHEMA

    def __post_init__(self) -> None:
        _identity(self.request_identity, "IsolationDecision.request_identity")
        _identity(self.policy_identity, "IsolationDecision.policy_identity")
        if self.observation_identity is not None:
            _identity(
                self.observation_identity, "IsolationDecision.observation_identity"
            )
        if not isinstance(self.status, IsolationDecisionStatus):
            raise TypeError("status must be an IsolationDecisionStatus")
        if not isinstance(self.required_level, IsolationLevel):
            raise TypeError("required_level must be an IsolationLevel")
        if self.achieved_level is not None and not isinstance(
            self.achieved_level, IsolationLevel
        ):
            raise TypeError("achieved_level must be an IsolationLevel or None")
        required = _controls(
            self.required_controls, "IsolationDecision.required_controls"
        )
        missing = _controls(self.missing_controls, "IsolationDecision.missing_controls")
        if not set(missing).issubset(required):
            fail(
                "IsolationDecision.missing_controls",
                "must be a subset of required_controls",
            )
        _portable_name(self.reason_code, "IsolationDecision.reason_code")
        if (
            self.authentication
            is not IsolationEvidenceAuthentication.UNAUTHENTICATED_LOCAL
        ):
            fail(
                "IsolationDecision.authentication",
                "the local contract cannot claim authenticated evidence",
            )
        if (self.observation_identity is None) != (self.achieved_level is None):
            fail(
                "IsolationDecision",
                "observation_identity and achieved_level must be present together",
            )
        if not set(mandatory_controls_for(self.required_level)).issubset(required):
            fail(
                "IsolationDecision.required_controls",
                "must include controls intrinsic to required_level",
            )
        allowed_reasons = {
            IsolationDecisionStatus.REPORTED_SUFFICIENT: {
                "containment.reported-sufficient",
                "containment.host-yolo-reported-only",
            },
            IsolationDecisionStatus.REJECTED: {
                "containment.host-yolo-policy-denied",
                "containment.host-yolo-acknowledgement-required",
                "containment.observation-binding-mismatch",
                "containment.observation-target-mismatch",
                "containment.enforcement-incomplete",
            },
            IsolationDecisionStatus.UNSUPPORTED: {
                "containment.stage-unconfigured",
                "containment.backend-unavailable",
                "containment.isolation-level-unavailable",
                "containment.controls-unavailable",
            },
        }
        if self.reason_code not in allowed_reasons[self.status]:
            fail(
                "IsolationDecision.reason_code",
                "is inconsistent with status",
            )
        no_observation_reasons = {
            "containment.stage-unconfigured",
            "containment.backend-unavailable",
            "containment.host-yolo-policy-denied",
            "containment.host-yolo-acknowledgement-required",
        }
        if (self.reason_code in no_observation_reasons) != (
            self.observation_identity is None
        ):
            fail("IsolationDecision", "observation presence contradicts reason_code")
        if self.reason_code != "containment.controls-unavailable" and missing:
            fail(
                "IsolationDecision.missing_controls",
                "only containment.controls-unavailable may report missing controls",
            )
        if self.reason_code.startswith("containment.host-yolo-") and (
            self.required_level is not IsolationLevel.HOST_YOLO
        ):
            fail(
                "IsolationDecision.required_level",
                "host-yolo reason requires host-yolo",
            )
        sufficient_level = (
            self.achieved_level is not None
            and self.achieved_level.satisfies(self.required_level)
        )
        if self.reason_code == "containment.isolation-level-unavailable":
            if sufficient_level or missing:
                fail("IsolationDecision", "isolation-level result is contradictory")
        elif self.reason_code == "containment.controls-unavailable":
            if not sufficient_level or not missing:
                fail("IsolationDecision", "missing-control result is contradictory")
        elif self.status is IsolationDecisionStatus.REPORTED_SUFFICIENT:
            if not sufficient_level or missing:
                fail(
                    "IsolationDecision",
                    "reported-sufficient status requires sufficient reported facts",
                )
            if (self.reason_code == "containment.host-yolo-reported-only") != (
                self.required_level is IsolationLevel.HOST_YOLO
            ):
                fail(
                    "IsolationDecision.reason_code",
                    "host-yolo reports must use the host-yolo reason",
                )

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    @property
    def reported_sufficient(self) -> bool:
        return self.status is IsolationDecisionStatus.REPORTED_SUFFICIENT

    def require_exact_recomputation(
        self,
        request: IsolationRequest,
        policy: IsolationPolicy,
        observation: IsolationObservation | None,
    ) -> None:
        """Check deterministic reproduction without granting execution authority."""

        from .evaluation import evaluate_isolation_policy

        expected = evaluate_isolation_policy(request, policy, observation)
        if self != expected:
            raise IsolationPolicyError("containment.decision-mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity,
            "policy_identity": self.policy_identity,
            "observation_identity": self.observation_identity,
            "status": self.status.value,
            "required_level": self.required_level.value,
            "achieved_level": (
                None if self.achieved_level is None else self.achieved_level.value
            ),
            "required_controls": [item.value for item in self.required_controls],
            "missing_controls": [item.value for item in self.missing_controls],
            "reason_code": self.reason_code,
            "authentication": self.authentication.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> IsolationDecision:
        data = contract_fields(
            value,
            path="IsolationDecision",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "request_identity",
                    "policy_identity",
                    "observation_identity",
                    "status",
                    "required_level",
                    "achieved_level",
                    "required_controls",
                    "missing_controls",
                    "reason_code",
                    "authentication",
                }
            ),
        )
        achieved = optional_string(
            data["achieved_level"], "IsolationDecision.achieved_level"
        )
        observation_identity = optional_string(
            data["observation_identity"], "IsolationDecision.observation_identity"
        )
        return cls(
            request_identity=_identity(
                data["request_identity"], "IsolationDecision.request_identity"
            ),
            policy_identity=_identity(
                data["policy_identity"], "IsolationDecision.policy_identity"
            ),
            observation_identity=(
                None
                if observation_identity is None
                else _identity(
                    observation_identity, "IsolationDecision.observation_identity"
                )
            ),
            status=enum_value(
                IsolationDecisionStatus, data["status"], "IsolationDecision.status"
            ),
            required_level=enum_value(
                IsolationLevel,
                data["required_level"],
                "IsolationDecision.required_level",
            ),
            achieved_level=(
                None
                if achieved is None
                else enum_value(
                    IsolationLevel, achieved, "IsolationDecision.achieved_level"
                )
            ),
            required_controls=_parse_controls(
                data["required_controls"], "IsolationDecision.required_controls"
            ),
            missing_controls=_parse_controls(
                data["missing_controls"], "IsolationDecision.missing_controls"
            ),
            reason_code=_portable_name(
                data["reason_code"], "IsolationDecision.reason_code"
            ),
            authentication=enum_value(
                IsolationEvidenceAuthentication,
                data["authentication"],
                "IsolationDecision.authentication",
            ),
        )


__all__ = [
    "ContainmentControl",
    "ContainmentStage",
    "IsolationDecision",
    "IsolationDecisionStatus",
    "IsolationEvidenceAuthentication",
    "IsolationLevel",
    "IsolationObservation",
    "IsolationPolicy",
    "IsolationPolicyError",
    "IsolationRequest",
    "StageIsolationRule",
    "mandatory_controls_for",
]
