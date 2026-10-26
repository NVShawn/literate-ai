"""Deterministic, bounded scheduling for exact lifecycle-action DAGs."""

from __future__ import annotations

import heapq
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from literate_ai.application.action_dispatch_policy import (
    ActionDispatchPolicy,
    ActionResources,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


class ActionDagSchedulingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class LifecycleActionKind(StrEnum):
    GENERATE = "generate"
    INDEX = "index"
    BUILD_INTENT = "build_intent"
    AUTHORIZE = "authorize"
    PLAN = "plan"
    BUILD = "build"
    TEST = "test"
    EXECUTE = "execute"
    ACCEPT = "accept"
    LINK = "link"
    PACKAGE = "package"
    FINALIZE = "finalize"


class LifecycleActionDisposition(StrEnum):
    ACCEPTED = "accepted"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERED = "recovered"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class LifecycleActionNode:
    """One identity-bound action and its exact predecessor/worker closure."""

    action_id: str
    component_revision: ContentIdentity
    kind: LifecycleActionKind
    payload_identity: ContentIdentity
    predecessor_ids: tuple[str, ...]
    eligible_worker_ids: tuple[str, ...]
    cache_affinity_worker_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.action_id, str)
            or not self.action_id
            or len(self.action_id) > 4096
            or not isinstance(self.component_revision, ContentIdentity)
            or not isinstance(self.kind, LifecycleActionKind)
            or not isinstance(self.payload_identity, ContentIdentity)
        ):
            raise ActionDagSchedulingError(
                "action_dag.node_invalid", "action node authority is not typed"
            )
        for values, label, required in (
            (self.predecessor_ids, "predecessors", False),
            (self.eligible_worker_ids, "eligible workers", True),
            (self.cache_affinity_worker_ids, "cache affinity", False),
        ):
            if (
                not isinstance(values, tuple)
                or (required and not values)
                or any(not isinstance(item, str) or not item for item in values)
                or values != tuple(sorted(set(values)))
            ):
                raise ActionDagSchedulingError(
                    "action_dag.node_invalid",
                    f"action node {label} must be canonical and unique",
                )
        if not set(self.cache_affinity_worker_ids) <= set(self.eligible_worker_ids):
            raise ActionDagSchedulingError(
                "action_dag.affinity_ineligible",
                "cache affinity may name only eligible workers",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/lifecycle-action-node@1",
                "action_id": self.action_id,
                "component_revision": self.component_revision.uri,
                "kind": self.kind.value,
                "payload_identity": self.payload_identity.uri,
                "predecessor_ids": list(self.predecessor_ids),
                "eligible_worker_ids": list(self.eligible_worker_ids),
                "cache_affinity_worker_ids": list(self.cache_affinity_worker_ids),
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleActionWorker:
    worker_id: str
    worker_identity: ContentIdentity
    catalog_identity: ContentIdentity
    observation_identity: ContentIdentity
    slots: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker_id, str)
            or not self.worker_id
            or any(
                not isinstance(value, ContentIdentity)
                for value in (
                    self.worker_identity,
                    self.catalog_identity,
                    self.observation_identity,
                )
            )
            or isinstance(self.slots, bool)
            or not isinstance(self.slots, int)
            or not 1 <= self.slots <= 256
        ):
            raise ActionDagSchedulingError(
                "action_dag.worker_invalid", "worker slots and authority must be typed"
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/lifecycle-action-worker@1",
                "worker_id": self.worker_id,
                "worker_identity": self.worker_identity.uri,
                "catalog_identity": self.catalog_identity.uri,
                "observation_identity": self.observation_identity.uri,
                "slots": self.slots,
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleActionDispatchRequest:
    schedule_identity: ContentIdentity
    action: LifecycleActionNode
    worker: LifecycleActionWorker
    slot: int
    predecessor_result_identities: tuple[ContentIdentity, ...]
    deadline_identity: ContentIdentity
    input_record_identities: tuple[ContentIdentity, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schedule_identity, ContentIdentity)
            or not isinstance(self.action, LifecycleActionNode)
            or not isinstance(self.worker, LifecycleActionWorker)
            or isinstance(self.slot, bool)
            or not isinstance(self.slot, int)
            or not 0 <= self.slot < self.worker.slots
            or not isinstance(self.deadline_identity, ContentIdentity)
            or any(
                not isinstance(item, ContentIdentity)
                for item in (
                    *self.predecessor_result_identities,
                    *self.input_record_identities,
                )
            )
            or not isinstance(self.input_record_identities, tuple)
            or len(set(self.input_record_identities))
            != len(self.input_record_identities)
        ):
            raise ActionDagSchedulingError(
                "action_dag.request_invalid", "dispatch request is not typed"
            )
        if self.worker.worker_id not in self.action.eligible_worker_ids:
            raise ActionDagSchedulingError(
                "action_dag.worker_ineligible", "dispatch selected an ineligible worker"
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": (
                    "literate-ai/lifecycle-action-dispatch-request@2"
                    if self.input_record_identities
                    else "literate-ai/lifecycle-action-dispatch-request@1"
                ),
                **(
                    {
                        "input_record_identities": [
                            item.uri for item in self.input_record_identities
                        ]
                    }
                    if self.input_record_identities
                    else {}
                ),
                "schedule_identity": self.schedule_identity.uri,
                "action_identity": self.action.identity.uri,
                "worker_identity": self.worker.identity.uri,
                "slot": self.slot,
                "predecessor_result_identities": [
                    item.uri for item in self.predecessor_result_identities
                ],
                "deadline_identity": self.deadline_identity.uri,
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleActionTermination:
    """Trusted owner evidence; never deserialize a transport claim into this proof."""

    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    evidence_identity: ContentIdentity
    kind: str
    exit_status: int | None = None
    fenced_lease_identity: ContentIdentity | None = None
    fencing_policy_identity: ContentIdentity | None = None

    def __post_init__(self):
        if any(
            not isinstance(value, ContentIdentity)
            for value in (
                self.request_identity,
                self.worker_identity,
                self.evidence_identity,
            )
        ):
            raise ActionDagSchedulingError(
                "action_dag.termination_invalid", "termination authority must be typed"
            )
        if self.kind in {"owned-terminal", "owned-cancelled"}:
            if (
                type(self.exit_status) is not int
                or self.fenced_lease_identity is not None
                or self.fencing_policy_identity is not None
            ):
                raise ActionDagSchedulingError(
                    "action_dag.termination_invalid",
                    "owned termination requires an actual terminal exit "
                    "and no inferred lease",
                )
        elif self.kind == "fenced-lease":
            if (
                self.exit_status is not None
                or not isinstance(self.fenced_lease_identity, ContentIdentity)
                or not isinstance(self.fencing_policy_identity, ContentIdentity)
            ):
                raise ActionDagSchedulingError(
                    "action_dag.termination_invalid",
                    "fenced loss requires verified lease and policy custody",
                )
        else:
            raise ActionDagSchedulingError(
                "action_dag.termination_invalid",
                "silence or timeout is not termination proof",
            )


@dataclass(frozen=True, slots=True)
class LifecycleActionProducerFailure:
    request_identity: ContentIdentity
    producer_request_identity: ContentIdentity
    evidence_identity: ContentIdentity | None = None

    def __post_init__(self):
        if (
            not isinstance(self.request_identity, ContentIdentity)
            or not isinstance(self.producer_request_identity, ContentIdentity)
            or self.evidence_identity is not None
            and not isinstance(self.evidence_identity, ContentIdentity)
        ):
            raise ActionDagSchedulingError(
                "action_dag.failure_correlation_invalid",
                "producer failure correlation must contain typed references",
            )

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/coalesced-producer-failure@1",
                "request_identity": self.request_identity.uri,
                "producer_request_identity": self.producer_request_identity.uri,
                "evidence_identity": None
                if self.evidence_identity is None
                else self.evidence_identity.uri,
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleActionNoActuation:
    """Trusted adapter proof that a failed waiter never started an owned process.

    This is controller routing evidence, never native terminal/fencing authority.
    No transport deserializer may mint it from a worker's claim.
    """

    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    producer_failure_identity: ContentIdentity

    def __post_init__(self):
        if any(
            not isinstance(value, ContentIdentity)
            for value in (
                self.request_identity,
                self.worker_identity,
                self.producer_failure_identity,
            )
        ):
            raise ActionDagSchedulingError(
                "action_dag.no_actuation_invalid", "no-actuation evidence must be typed"
            )

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/coalesced-waiter-not-actuated@1",
                "request_identity": self.request_identity.uri,
                "worker_identity": self.worker_identity.uri,
                "producer_failure_identity": self.producer_failure_identity.uri,
                "owned_process_started": False,
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleActionDispatchOutcome:
    request_identity: ContentIdentity
    result_identity: ContentIdentity | None
    failure_code: str | None = None
    termination: LifecycleActionTermination | None = None
    producer_failure: LifecycleActionProducerFailure | None = None
    no_actuation: LifecycleActionNoActuation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_identity, ContentIdentity):
            raise ActionDagSchedulingError(
                "action_dag.outcome_invalid", "outcome request identity is not typed"
            )
        if (self.result_identity is None) == (self.failure_code is None):
            raise ActionDagSchedulingError(
                "action_dag.outcome_invalid",
                "outcome must contain exactly one result identity or failure",
            )
        if self.producer_failure is not None and (
            not isinstance(self.producer_failure, LifecycleActionProducerFailure)
            or self.producer_failure.request_identity != self.request_identity
            or self.failure_code is None
        ):
            raise ActionDagSchedulingError(
                "action_dag.failure_correlation_invalid",
                "producer failure must bind this failed request",
            )
        if self.no_actuation is not None and (
            not isinstance(self.no_actuation, LifecycleActionNoActuation)
            or self.no_actuation.request_identity != self.request_identity
            or self.producer_failure is None
            or self.no_actuation.producer_failure_identity
            != self.producer_failure.identity
            or self.termination is not None
            or self.failure_code is None
        ):
            raise ActionDagSchedulingError(
                "action_dag.no_actuation_invalid",
                "no-actuation proof must bind this failed waiter and correlation",
            )
        if self.termination is not None and (
            not isinstance(self.termination, LifecycleActionTermination)
            or self.termination.request_identity != self.request_identity
            or self.failure_code is None
        ):
            raise ActionDagSchedulingError(
                "action_dag.termination_invalid",
                "termination must bind this failed dispatch",
            )
        if self.result_identity is not None and not isinstance(
            self.result_identity, ContentIdentity
        ):
            raise ActionDagSchedulingError(
                "action_dag.outcome_invalid", "outcome result identity is not typed"
            )
        if self.failure_code is not None and (
            not isinstance(self.failure_code, str)
            or not self.failure_code
            or len(self.failure_code) > 127
        ):
            raise ActionDagSchedulingError(
                "action_dag.outcome_invalid", "outcome failure code is invalid"
            )


@dataclass(frozen=True, slots=True)
class LifecycleActionRecoveryCandidate:
    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    result_identity: ContentIdentity

    def __post_init__(self) -> None:
        if any(
            not isinstance(item, ContentIdentity)
            for item in (
                self.request_identity,
                self.worker_identity,
                self.result_identity,
            )
        ):
            raise ActionDagSchedulingError(
                "action_dag.recovery_invalid",
                "recovery authority must contain typed identities",
            )


class LifecycleActionDispatcher(Protocol):
    """Serializable action actuation boundary; no controller callback is supplied."""

    def dispatch(
        self, request: LifecycleActionDispatchRequest
    ) -> LifecycleActionDispatchOutcome: ...

    def cancel(self, request: LifecycleActionDispatchRequest) -> None: ...


@dataclass(frozen=True, slots=True)
class LifecycleActionResult:
    action_id: str
    action_identity: ContentIdentity
    request_identity: ContentIdentity | None
    worker_identity: ContentIdentity | None
    disposition: LifecycleActionDisposition
    result_identity: ContentIdentity | None
    failure_code: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.action_id, str)
            or not self.action_id
            or not isinstance(self.action_identity, ContentIdentity)
            or not isinstance(self.disposition, LifecycleActionDisposition)
            or (
                self.request_identity is not None
                and not isinstance(self.request_identity, ContentIdentity)
            )
            or (
                self.worker_identity is not None
                and not isinstance(self.worker_identity, ContentIdentity)
            )
            or (
                self.result_identity is not None
                and not isinstance(self.result_identity, ContentIdentity)
            )
            or (
                self.failure_code is not None
                and (not isinstance(self.failure_code, str) or not self.failure_code)
            )
        ):
            raise ActionDagSchedulingError(
                "action_dag.result_invalid", "action result is not typed"
            )
        succeeded = self.disposition in {
            LifecycleActionDisposition.ACCEPTED,
            LifecycleActionDisposition.RECOVERED,
        }
        if succeeded != (self.result_identity is not None) or succeeded == (
            self.failure_code is not None
        ):
            raise ActionDagSchedulingError(
                "action_dag.result_invalid",
                "result disposition does not match its evidence",
            )
        dispatched = self.disposition not in {
            LifecycleActionDisposition.CANCELLED,
            LifecycleActionDisposition.BLOCKED,
        }
        if dispatched != (
            self.request_identity is not None and self.worker_identity is not None
        ):
            raise ActionDagSchedulingError(
                "action_dag.result_invalid",
                "only dispatched results may bind request and worker identities",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/lifecycle-action-result@1",
                "action_id": self.action_id,
                "action_identity": self.action_identity.uri,
                "request_identity": (
                    None if self.request_identity is None else self.request_identity.uri
                ),
                "worker_identity": (
                    None if self.worker_identity is None else self.worker_identity.uri
                ),
                "disposition": self.disposition.value,
                "result_identity": (
                    None if self.result_identity is None else self.result_identity.uri
                ),
                "failure_code": self.failure_code,
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleActionAttempt:
    number: int
    result: LifecycleActionResult
    termination: LifecycleActionTermination | None = None
    producer_failure: LifecycleActionProducerFailure | None = None
    no_actuation: LifecycleActionNoActuation | None = None

    def __post_init__(self) -> None:
        if (
            type(self.number) is not int
            or not 1 <= self.number <= 16
            or not isinstance(self.result, LifecycleActionResult)
            or self.result.disposition
            not in {
                LifecycleActionDisposition.ACCEPTED,
                LifecycleActionDisposition.FAILED,
                LifecycleActionDisposition.UNKNOWN,
            }
        ):
            raise ActionDagSchedulingError(
                "action_dag.attempt_invalid",
                "attempt must bind an actual dispatched result",
            )

        if self.termination is not None and (
            not isinstance(self.termination, LifecycleActionTermination)
            or self.termination.request_identity != self.result.request_identity
            or self.termination.worker_identity != self.result.worker_identity
        ):
            raise ActionDagSchedulingError(
                "action_dag.termination_invalid",
                "attempt termination differs from dispatched authority",
            )

        if self.producer_failure is not None and (
            not isinstance(self.producer_failure, LifecycleActionProducerFailure)
            or self.producer_failure.request_identity != self.result.request_identity
        ):
            raise ActionDagSchedulingError(
                "action_dag.failure_correlation_invalid",
                "attempt producer correlation differs",
            )

        if self.no_actuation is not None and (
            not isinstance(self.no_actuation, LifecycleActionNoActuation)
            or self.no_actuation.request_identity != self.result.request_identity
            or self.no_actuation.worker_identity != self.result.worker_identity
            or self.producer_failure is None
            or self.no_actuation.producer_failure_identity
            != self.producer_failure.identity
            or self.termination is not None
        ):
            raise ActionDagSchedulingError(
                "action_dag.no_actuation_invalid", "attempt no-actuation proof differs"
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/lifecycle-action-attempt@3"
                if self.no_actuation
                else "literate-ai/lifecycle-action-attempt@2"
                if self.termination or self.producer_failure
                else "literate-ai/lifecycle-action-attempt@1",
                **(
                    {"no_actuation_identity": self.no_actuation.identity.uri}
                    if self.no_actuation
                    else {}
                ),
                **(
                    {"producer_failure_identity": self.producer_failure.identity.uri}
                    if self.producer_failure
                    else {}
                ),
                **(
                    {
                        "termination_evidence_identity": (
                            self.termination.evidence_identity.uri
                        ),
                        "termination_kind": self.termination.kind,
                    }
                    if self.termination
                    else {}
                ),
                "number": self.number,
                "result_identity": self.result.identity.uri,
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleActionUnresolvedOwnership:
    request: LifecycleActionDispatchRequest
    policy: ActionDispatchPolicy

    def __post_init__(self):
        if not isinstance(
            self.request, LifecycleActionDispatchRequest
        ) or not isinstance(self.policy, ActionDispatchPolicy):
            raise ActionDagSchedulingError(
                "action_dag.ownership_invalid",
                "unresolved ownership must retain exact request and policy",
            )

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/unresolved-action-ownership@1",
                "request_identity": self.request.identity.uri,
                "worker_identity": self.request.worker.worker_identity.uri,
                "policy_identity": self.policy.identity.uri,
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleActionScheduleResult:
    schedule_identity: ContentIdentity
    max_parallelism: int
    results: tuple[LifecycleActionResult, ...]
    attempts: tuple[LifecycleActionAttempt, ...] = ()
    unresolved_ownership: tuple[LifecycleActionUnresolvedOwnership, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schedule_identity, ContentIdentity)
            or isinstance(self.max_parallelism, bool)
            or not isinstance(self.max_parallelism, int)
            or not 1 <= self.max_parallelism <= 256
            or not self.results
            or any(not isinstance(item, LifecycleActionResult) for item in self.results)
            or tuple(item.action_id for item in self.results)
            != tuple(sorted({item.action_id for item in self.results}))
        ):
            raise ActionDagSchedulingError(
                "action_dag.schedule_result_invalid",
                "schedule result is not canonical and typed",
            )

        if not isinstance(self.attempts, tuple) or any(
            not isinstance(item, LifecycleActionAttempt) for item in self.attempts
        ):
            raise ActionDagSchedulingError(
                "action_dag.attempt_invalid", "schedule attempts must be typed"
            )
        keys = tuple((item.result.action_id, item.number) for item in self.attempts)
        if keys != tuple(sorted(set(keys))):
            raise ActionDagSchedulingError(
                "action_dag.attempt_invalid",
                "schedule attempts must be canonical and unique",
            )
        final = {item.action_id: item for item in self.results}
        grouped = defaultdict(list)
        for attempt in self.attempts:
            grouped[attempt.result.action_id].append(attempt)
        for action_id, values in grouped.items():
            last = values[-1].result
            current = final.get(action_id)
            # A queued retry that could not be placed (blocked by unresolved
            # ownership, or cancelled) keeps its definite failed attempts and
            # ends undispatched; it never claims another attempt ran.
            unplaced_retry = (
                current is not None
                and current.disposition
                in {
                    LifecycleActionDisposition.BLOCKED,
                    LifecycleActionDisposition.CANCELLED,
                }
                and last.disposition is LifecycleActionDisposition.FAILED
            )
            if tuple(item.number for item in values) != tuple(
                range(1, len(values) + 1)
            ) or (current != last and not unplaced_retry):
                raise ActionDagSchedulingError(
                    "action_dag.attempt_invalid",
                    "attempt sequence must retain the final result",
                )

        if (
            not isinstance(self.unresolved_ownership, tuple)
            or any(
                not isinstance(item, LifecycleActionUnresolvedOwnership)
                for item in self.unresolved_ownership
            )
            or tuple(item.request.identity.uri for item in self.unresolved_ownership)
            != tuple(
                sorted(
                    {item.request.identity.uri for item in self.unresolved_ownership}
                )
            )
        ):
            raise ActionDagSchedulingError(
                "action_dag.ownership_invalid",
                "unresolved ownership must be canonical and typed",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/lifecycle-action-schedule-result@3"
                if self.unresolved_ownership
                else "literate-ai/lifecycle-action-schedule-result@2"
                if self.attempts
                else "literate-ai/lifecycle-action-schedule-result@1",
                **(
                    {
                        "attempt_identities": [
                            attempt.identity.uri for attempt in self.attempts
                        ]
                    }
                    if self.attempts
                    else {}
                ),
                **(
                    {
                        "unresolved_ownership": [
                            item.identity.uri for item in self.unresolved_ownership
                        ]
                    }
                    if self.unresolved_ownership
                    else {}
                ),
                "schedule_identity": self.schedule_identity.uri,
                "max_parallelism": self.max_parallelism,
                "result_identities": [item.identity.uri for item in self.results],
            }
        )


class LifecycleActionDagScheduler:
    """Run every ready action on stable compatible worker slots."""

    def __init__(self):
        self._unresolved_ownership = {}

    @property
    def unresolved_ownership(self):
        return tuple(
            self._unresolved_ownership[key]
            for key in sorted(self._unresolved_ownership, key=lambda key: key.uri)
        )

    def resolve_ownership(self, termination: LifecycleActionTermination) -> None:
        """Apply trusted owner/fence proof for subsequent runs of this coordinator.

        This is not remote lease discovery or a cross-controller lock. A new
        controller must restore its verifier-owned unresolved journal before
        production admission; this in-memory slice cannot replace that service.
        """
        if not isinstance(termination, LifecycleActionTermination):
            raise ActionDagSchedulingError(
                "action_dag.termination_invalid",
                "ownership resolution requires trusted typed evidence",
            )
        pending = self._unresolved_ownership.get(termination.request_identity)
        if (
            pending is None
            or pending.request.worker.worker_identity != termination.worker_identity
            or termination.kind == "fenced-lease"
            and termination.fencing_policy_identity
            != pending.policy.loss_fencing_policy_identity
        ):
            raise ActionDagSchedulingError(
                "action_dag.ownership_invalid",
                "resolution does not bind the held request/worker/fencing policy",
            )
        self._unresolved_ownership.pop(termination.request_identity)

    def run(
        self,
        nodes: Sequence[LifecycleActionNode],
        workers: Sequence[LifecycleActionWorker],
        dispatcher: LifecycleActionDispatcher,
        *,
        deadline_identity: ContentIdentity,
        max_parallelism: int | None = None,
        recovery: Mapping[str, LifecycleActionRecoveryCandidate] | None = None,
        action_policies: Mapping[str, ActionDispatchPolicy] | None = None,
        worker_resources: Mapping[str, ActionResources] | None = None,
    ) -> LifecycleActionScheduleResult:
        if not isinstance(deadline_identity, ContentIdentity):
            raise ActionDagSchedulingError(
                "action_dag.deadline_invalid", "deadline identity is not typed"
            )
        node_map, worker_map, successors = self._preflight(nodes, workers)
        policies = {} if action_policies is None else dict(action_policies)
        capacities = {} if worker_resources is None else dict(worker_resources)
        if (
            set(policies) - set(node_map)
            or set(capacities) - set(worker_map)
            or any(not isinstance(x, ActionDispatchPolicy) for x in policies.values())
            or any(not isinstance(x, ActionResources) for x in capacities.values())
        ):
            raise ActionDagSchedulingError(
                "action_dag.policy_invalid",
                "resource policy names unknown or untyped authority",
            )
        for action_id, policy in policies.items():
            if policy.max_attempts > 1 and node_map[action_id].kind not in {
                LifecycleActionKind.BUILD,
                LifecycleActionKind.TEST,
            }:
                raise ActionDagSchedulingError(
                    "action_dag.retry_unsafe",
                    "only declared safe BUILD/TEST actions may retry",
                )
            if any(policy.resources.values) and not any(
                worker_id in capacities
                and all(
                    demand <= capacity
                    for demand, capacity in zip(
                        policy.resources.values,
                        capacities[worker_id].values,
                        strict=True,
                    )
                )
                for worker_id in node_map[action_id].eligible_worker_ids
            ):
                raise ActionDagSchedulingError(
                    "action_dag.capacity_insufficient",
                    "no eligible worker can reserve the action resource demand",
                )
        total_slots = sum(item.slots for item in worker_map.values())
        limit = total_slots if max_parallelism is None else max_parallelism
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 256
        ):
            raise ActionDagSchedulingError(
                "action_dag.parallelism_invalid",
                "parallelism must be between one and 256",
            )
        limit = min(limit, total_slots)
        recovery_map = {} if recovery is None else dict(recovery)
        if set(recovery_map) - set(node_map) or any(
            not isinstance(item, LifecycleActionRecoveryCandidate)
            for item in recovery_map.values()
        ):
            raise ActionDagSchedulingError(
                "action_dag.recovery_invalid", "recovery does not match the action set"
            )
        schedule_identity = canonical_identity(
            {
                "schema": (
                    "literate-ai/lifecycle-action-schedule@2"
                    if policies or capacities
                    else "literate-ai/lifecycle-action-schedule@1"
                ),
                **(
                    {
                        "action_policies": {
                            key: policies[key].identity.uri for key in sorted(policies)
                        },
                        "worker_resources": {
                            key: capacities[key].to_dict() for key in sorted(capacities)
                        },
                    }
                    if policies or capacities
                    else {}
                ),
                "nodes": [node_map[key].identity.uri for key in sorted(node_map)],
                "workers": [worker_map[key].identity.uri for key in sorted(worker_map)],
                "deadline_identity": deadline_identity.uri,
                "max_parallelism": limit,
            }
        )
        remaining = {key: len(node.predecessor_ids) for key, node in node_map.items()}
        ready: list[tuple[str, str]] = []
        for key, count in remaining.items():
            if count == 0:
                heapq.heappush(ready, (node_map[key].identity.uri, key))
        free_slots = {
            worker_id: set(range(worker.slots))
            for worker_id, worker in worker_map.items()
        }
        active: dict[
            Future[LifecycleActionDispatchOutcome],
            tuple[str, LifecycleActionDispatchRequest],
        ] = {}
        results: dict[str, LifecycleActionResult] = {}

        attempt_counts = {key: 0 for key in node_map}
        tried_workers = {key: {} for key in node_map}
        attempts = {}
        record_attempts = any(policy.max_attempts > 1 for policy in policies.values())
        reserved = {key: [0, 0, 0, 0] for key in worker_map}
        pending_ownership = dict(self._unresolved_ownership)
        quarantined = {
            item.request.worker.worker_id for item in pending_ownership.values()
        }
        held_claims: set[str] = {
            claim
            for item in pending_ownership.values()
            for claim in item.policy.exclusive_claims
        }

        def reserve(action_id: str, worker_id: str, direction: int) -> None:
            policy = policies.get(action_id, ActionDispatchPolicy())
            for index, amount in enumerate(policy.resources.values):
                reserved[worker_id][index] += direction * amount
            if direction > 0:
                held_claims.update(policy.exclusive_claims)
            else:
                held_claims.difference_update(policy.exclusive_claims)

        def fits(node: LifecycleActionNode, worker_id: str) -> bool:
            policy = policies.get(node.action_id, ActionDispatchPolicy())
            if held_claims.intersection(policy.exclusive_claims):
                return False
            if not any(policy.resources.values):
                return True
            return worker_id in capacities and all(
                used + demand <= capacity
                for used, demand, capacity in zip(
                    reserved[worker_id],
                    policy.resources.values,
                    capacities[worker_id].values,
                    strict=True,
                )
            )

        def worker_slot(node: LifecycleActionNode) -> tuple[str, int] | None:
            affinity = set(node.cache_affinity_worker_ids)
            candidates = sorted(
                (
                    (
                        tried_workers[node.action_id].get(worker_id, 0),
                        0 if worker_id in affinity else 1,
                        worker_id,
                        min(slots),
                    )
                    for worker_id, slots in free_slots.items()
                    if slots
                    and worker_id in node.eligible_worker_ids
                    and worker_id not in quarantined
                    and fits(node, worker_id)
                )
            )
            if not candidates:
                return None
            _, _, worker_id, slot = candidates[0]
            return worker_id, slot

        def cancel_descendants(failed: str) -> None:
            pending = deque(successors[failed])
            while pending:
                child = pending.popleft()
                if child in results:
                    continue
                node = node_map[child]
                results[child] = LifecycleActionResult(
                    child,
                    node.identity,
                    None,
                    None,
                    LifecycleActionDisposition.CANCELLED,
                    None,
                    "dependency-failed",
                )
                pending.extend(successors[child])

        with ThreadPoolExecutor(max_workers=limit) as pool:
            while len(results) < len(node_map):
                deferred: list[tuple[str, str]] = []
                while ready and len(active) < limit:
                    _, action_id = heapq.heappop(ready)
                    if action_id in results:
                        continue
                    node = node_map[action_id]
                    selected = worker_slot(node)
                    if selected is None:
                        deferred.append((node.identity.uri, action_id))
                        continue
                    worker_id, slot = selected
                    predecessor_results = tuple(
                        results[parent].result_identity
                        for parent in node.predecessor_ids
                    )
                    if any(item is None for item in predecessor_results):
                        raise ActionDagSchedulingError(
                            "action_dag.predecessor_missing",
                            "ready action lacks accepted predecessor evidence",
                        )
                    attempt_number = attempt_counts[action_id] + 1
                    request = LifecycleActionDispatchRequest(
                        schedule_identity
                        if attempt_number == 1
                        else canonical_identity(
                            {
                                "schema": (
                                    "literate-ai/lifecycle-action-attempt-schedule@1"
                                ),
                                "schedule_identity": schedule_identity.uri,
                                "action_identity": node.identity.uri,
                                "attempt": attempt_number,
                            }
                        ),
                        node,
                        worker_map[worker_id],
                        slot,
                        tuple(
                            item
                            for item in predecessor_results
                            if isinstance(item, ContentIdentity)
                        ),
                        deadline_identity,
                    )
                    free_slots[worker_id].remove(slot)
                    reserve(action_id, worker_id, 1)
                    candidate = recovery_map.get(action_id)
                    if candidate is not None:
                        if (
                            candidate.request_identity != request.identity
                            or candidate.worker_identity
                            != request.worker.worker_identity
                        ):
                            raise ActionDagSchedulingError(
                                "action_dag.recovery_stale",
                                "recovery route or request identity changed",
                            )
                        results[action_id] = LifecycleActionResult(
                            action_id,
                            node.identity,
                            request.identity,
                            request.worker.worker_identity,
                            LifecycleActionDisposition.RECOVERED,
                            candidate.result_identity,
                            None,
                        )
                        free_slots[worker_id].add(slot)
                        reserve(action_id, worker_id, -1)
                        for child in successors[action_id]:
                            remaining[child] -= 1
                            if remaining[child] == 0:
                                child_node = node_map[child]
                                heapq.heappush(ready, (child_node.identity.uri, child))
                        continue
                    attempt_counts[action_id] = attempt_number
                    tried_workers[action_id][worker_id] = (
                        tried_workers[action_id].get(worker_id, 0) + 1
                    )
                    # Retain every actuated request before handing control to a
                    # dispatcher. An unrelated malformed result can abort this
                    # schedule while other remote requests remain unresolved.
                    self._unresolved_ownership[request.identity] = (
                        LifecycleActionUnresolvedOwnership(
                            request, policies.get(action_id, ActionDispatchPolicy())
                        )
                    )
                    active[pool.submit(dispatcher.dispatch, request)] = (
                        action_id,
                        request,
                    )
                for item in deferred:
                    heapq.heappush(ready, item)
                if not active:
                    if ready and pending_ownership:
                        while ready:
                            _, blocked = heapq.heappop(ready)
                            if blocked in results:
                                continue
                            results[blocked] = LifecycleActionResult(
                                blocked,
                                node_map[blocked].identity,
                                None,
                                None,
                                LifecycleActionDisposition.BLOCKED,
                                None,
                                "worker-ownership-unresolved",
                            )
                            cancel_descendants(blocked)
                        continue
                    if ready:
                        raise ActionDagSchedulingError(
                            "action_dag.worker_unavailable",
                            "ready actions have no compatible free worker slot",
                        )
                    if len(results) < len(node_map):
                        raise ActionDagSchedulingError(
                            "action_dag.deadlock", "action DAG made no progress"
                        )
                    break
                completed, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                ordered = sorted(completed, key=lambda item: active[item][0])
                for future in ordered:
                    action_id, request = active.pop(future)
                    node = node_map[action_id]
                    # Keep custody even when malformed dispatcher evidence raises
                    # before a normal failure result can be constructed.
                    pending = LifecycleActionUnresolvedOwnership(
                        request, policies.get(action_id, ActionDispatchPolicy())
                    )
                    self._unresolved_ownership[request.identity] = pending
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = LifecycleActionDispatchOutcome(
                            request.identity,
                            None,
                            getattr(exc, "code", None) or "dispatch-failed",
                            getattr(exc, "termination_evidence", None),
                            getattr(exc, "producer_failure", None),
                            getattr(exc, "no_actuation_evidence", None),
                        )
                    if (
                        not isinstance(outcome, LifecycleActionDispatchOutcome)
                        or outcome.request_identity != request.identity
                    ):
                        raise ActionDagSchedulingError(
                            "action_dag.outcome_mismatch",
                            "dispatcher returned evidence for another request",
                        )
                    if (
                        outcome.termination is not None
                        and outcome.termination.worker_identity
                        != request.worker.worker_identity
                    ):
                        raise ActionDagSchedulingError(
                            "action_dag.termination_invalid",
                            "terminal evidence names another worker",
                        )
                    if (
                        outcome.termination is not None
                        and outcome.termination.kind == "fenced-lease"
                        and outcome.termination.fencing_policy_identity
                        != policies.get(
                            action_id, ActionDispatchPolicy()
                        ).loss_fencing_policy_identity
                    ):
                        raise ActionDagSchedulingError(
                            "action_dag.fencing_policy_invalid",
                            "worker loss does not bind the admitted fencing policy",
                        )
                    if (
                        outcome.no_actuation is not None
                        and outcome.no_actuation.worker_identity
                        != request.worker.worker_identity
                    ):
                        raise ActionDagSchedulingError(
                            "action_dag.no_actuation_invalid",
                            "no-actuation proof names another worker",
                        )
                    if outcome.failure_code is not None:
                        uncertain = (
                            outcome.termination is None and outcome.no_actuation is None
                        )
                        if uncertain:
                            pending = LifecycleActionUnresolvedOwnership(
                                request, policies.get(action_id, ActionDispatchPolicy())
                            )
                            pending_ownership[request.identity] = pending
                            self._unresolved_ownership[request.identity] = pending
                            quarantined.add(request.worker.worker_id)
                        else:
                            self._unresolved_ownership.pop(request.identity)
                            free_slots[request.worker.worker_id].add(request.slot)
                            reserve(action_id, request.worker.worker_id, -1)
                        results[action_id] = LifecycleActionResult(
                            action_id,
                            node.identity,
                            request.identity,
                            request.worker.worker_identity,
                            LifecycleActionDisposition.UNKNOWN
                            if uncertain
                            else LifecycleActionDisposition.FAILED,
                            None,
                            outcome.failure_code,
                        )
                        attempts[(action_id, attempt_counts[action_id])] = (
                            LifecycleActionAttempt(
                                attempt_counts[action_id],
                                results[action_id],
                                outcome.termination,
                                outcome.producer_failure,
                                outcome.no_actuation,
                            )
                        )
                        policy = policies.get(action_id, ActionDispatchPolicy())
                        if (
                            outcome.failure_code in policy.retryable_failure_codes
                            and outcome.termination is not None
                            and (
                                outcome.termination.kind != "fenced-lease"
                                or outcome.termination.fencing_policy_identity
                                == policy.loss_fencing_policy_identity
                            )
                            and attempt_counts[action_id] < policy.max_attempts
                        ):
                            results.pop(action_id)
                            heapq.heappush(ready, (node.identity.uri, action_id))
                            continue
                        cancel_descendants(action_id)
                        continue
                    self._unresolved_ownership.pop(request.identity)
                    free_slots[request.worker.worker_id].add(request.slot)
                    reserve(action_id, request.worker.worker_id, -1)
                    assert outcome.result_identity is not None
                    results[action_id] = LifecycleActionResult(
                        action_id,
                        node.identity,
                        request.identity,
                        request.worker.worker_identity,
                        LifecycleActionDisposition.ACCEPTED,
                        outcome.result_identity,
                        None,
                    )
                    if record_attempts:
                        attempts[(action_id, attempt_counts[action_id])] = (
                            LifecycleActionAttempt(
                                attempt_counts[action_id], results[action_id]
                            )
                        )
                    for child in successors[action_id]:
                        if child in results:
                            continue
                        remaining[child] -= 1
                        if remaining[child] == 0:
                            child_node = node_map[child]
                            heapq.heappush(ready, (child_node.identity.uri, child))
        return LifecycleActionScheduleResult(
            schedule_identity,
            limit,
            tuple(results[key] for key in sorted(results)),
            tuple(attempts[key] for key in sorted(attempts)),
            tuple(
                pending_ownership[key]
                for key in sorted(pending_ownership, key=lambda key: key.uri)
            ),
        )

    @staticmethod
    def _preflight(
        nodes: Sequence[LifecycleActionNode],
        workers: Sequence[LifecycleActionWorker],
    ) -> tuple[
        dict[str, LifecycleActionNode],
        dict[str, LifecycleActionWorker],
        dict[str, tuple[str, ...]],
    ]:
        if not nodes or any(
            not isinstance(item, LifecycleActionNode) for item in nodes
        ):
            raise ActionDagSchedulingError(
                "action_dag.empty", "action DAG requires typed nodes"
            )
        node_map = {item.action_id: item for item in nodes}
        worker_map = {item.worker_id: item for item in workers}
        if (
            len(node_map) != len(nodes)
            or not workers
            or len(worker_map) != len(workers)
        ):
            raise ActionDagSchedulingError(
                "action_dag.noncanonical", "actions and workers must be unique"
            )
        worker_ids = set(worker_map)
        if any(
            not set(node.eligible_worker_ids) <= worker_ids
            for node in node_map.values()
        ):
            raise ActionDagSchedulingError(
                "action_dag.worker_unknown", "action names an unknown eligible worker"
            )
        successors: dict[str, list[str]] = defaultdict(list)
        remaining = dict(node_map)
        indegree: dict[str, int] = {}
        for action_id, node in node_map.items():
            if action_id in node.predecessor_ids or not set(
                node.predecessor_ids
            ) <= set(node_map):
                raise ActionDagSchedulingError(
                    "action_dag.edge_invalid",
                    "action predecessor is self-referential or unknown",
                )
            indegree[action_id] = len(node.predecessor_ids)
            for parent in node.predecessor_ids:
                successors[parent].append(action_id)
        queue = deque(sorted(key for key, count in indegree.items() if count == 0))
        visited = 0
        while queue:
            parent = queue.popleft()
            visited += 1
            remaining.pop(parent, None)
            for child in sorted(successors[parent]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if visited != len(node_map):
            raise ActionDagSchedulingError(
                "action_dag.cyclic",
                "action dependency graph contains a directed cycle",
            )
        return (
            node_map,
            worker_map,
            {key: tuple(sorted(value)) for key, value in successors.items()},
        )


__all__ = [
    "ActionDagSchedulingError",
    "LifecycleActionDagScheduler",
    "LifecycleActionAttempt",
    "LifecycleActionTermination",
    "LifecycleActionProducerFailure",
    "LifecycleActionUnresolvedOwnership",
    "LifecycleActionDispatchOutcome",
    "LifecycleActionDispatchRequest",
    "LifecycleActionDispatcher",
    "LifecycleActionDisposition",
    "LifecycleActionKind",
    "LifecycleActionNode",
    "LifecycleActionRecoveryCandidate",
    "LifecycleActionResult",
    "LifecycleActionScheduleResult",
    "LifecycleActionWorker",
]
