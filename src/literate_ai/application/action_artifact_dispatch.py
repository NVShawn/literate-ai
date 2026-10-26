"""Coalesce immutable payload production while retaining fresh request admission."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Protocol

from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDispatchOutcome,
    LifecycleActionDispatchRequest,
    LifecycleActionProducerFailure,
    LifecycleActionTermination,
)
from literate_ai.contracts.identity import ContentIdentity


class ActionArtifactExecutor(Protocol):
    """Producer owns verified CAS custody; admission verifies each exact request.

    A key must cover the complete compile input and output contract. Neither
    request identity nor package-selection metadata is a semantic action key.
    The executor must authorize execution and independently verify payload bytes,
    target compatibility and current receipt requirements during admission.
    """

    def action_key(
        self, request: LifecycleActionDispatchRequest
    ) -> ContentIdentity: ...
    def produce(self, request: LifecycleActionDispatchRequest) -> ContentIdentity: ...
    def admit(
        self, request: LifecycleActionDispatchRequest, artifact: ContentIdentity
    ) -> LifecycleActionDispatchOutcome: ...
    def cancel(self, request: LifecycleActionDispatchRequest) -> None: ...


class CoalescingArtifactDispatcher:
    """One producer per sound action key inside a finite coordinator run.

    Only immutable payload identities are shared. Each request still crosses
    fresh admission; an accepted outcome is never copied to another request.
    Producer failures reach every waiter and are evicted for an explicit retry.
    This is not a distributed lock or a persistent success/receipt cache.
    """

    def __init__(
        self,
        executor: ActionArtifactExecutor,
        *,
        loss_fencing_policy_identity: ContentIdentity | None = None,
    ) -> None:
        if loss_fencing_policy_identity is not None and not isinstance(
            loss_fencing_policy_identity, ContentIdentity
        ):
            raise TypeError("coalesced loss resolution requires a typed fencing policy")
        self.loss_fencing_policy_identity = loss_fencing_policy_identity
        self.executor = executor
        self._blocked_productions: dict[
            ContentIdentity, LifecycleActionDispatchRequest
        ] = {}
        self._lock = threading.Lock()
        self._artifacts: dict[ContentIdentity, Future[ContentIdentity]] = {}
        self._users: dict[
            ContentIdentity, dict[ContentIdentity, LifecycleActionDispatchRequest]
        ] = {}
        self._producers: dict[ContentIdentity, LifecycleActionDispatchRequest] = {}
        self._cancelled: set[ContentIdentity] = set()

    def dispatch(
        self, request: LifecycleActionDispatchRequest
    ) -> LifecycleActionDispatchOutcome:
        key = self.executor.action_key(request)
        if not isinstance(key, ContentIdentity):
            raise ActionDagSchedulingError(
                "action_dag.key_invalid", "artifact executor returned an untyped key"
            )
        with self._lock:
            if request.identity in self._cancelled:
                raise ActionDagSchedulingError(
                    "action_dag.cancelled", "artifact request was cancelled"
                )
            self._users.setdefault(key, {})[request.identity] = request
            future = self._artifacts.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._artifacts[key] = future
                self._producers[key] = request
            producer_request = self._producers.get(
                key
            ) or self._blocked_productions.get(key)
        assert future is not None
        try:
            if owner:
                try:
                    artifact = self.executor.produce(request)
                    if not isinstance(artifact, ContentIdentity):
                        raise ActionDagSchedulingError(
                            "action_dag.artifact_invalid",
                            "producer returned an untyped payload identity",
                        )
                except BaseException as exc:
                    future.set_exception(exc)
                    termination = getattr(exc, "termination_evidence", None)
                    terminal = (
                        isinstance(termination, LifecycleActionTermination)
                        and termination.request_identity == request.identity
                        and termination.worker_identity
                        == request.worker.worker_identity
                        and (
                            termination.kind != "fenced-lease"
                            or termination.fencing_policy_identity
                            == self.loss_fencing_policy_identity
                        )
                    )
                    with self._lock:
                        if terminal:
                            self._artifacts.pop(key, None)
                        else:
                            # Unknown production may still own remote resources.
                            # Preserve its failed Future to block this key on every
                            # worker until an explicit trusted owner/fence resolution.
                            self._blocked_productions[key] = request
                    raise
                else:
                    future.set_result(artifact)
                finally:
                    with self._lock:
                        if self._producers.get(key) == request:
                            self._producers.pop(key, None)
            try:
                artifact = future.result()
            except Exception as exc:
                if owner:
                    raise
                termination = getattr(exc, "termination_evidence", None)
                code = getattr(exc, "code", None) or "dispatch-failed"
                error = ActionDagSchedulingError(
                    code,
                    "coalesced artifact producer failed; "
                    "this request did not own its process",
                )
                if producer_request is not None:
                    error.producer_failure = LifecycleActionProducerFailure(
                        request.identity,
                        producer_request.identity,
                        termination.evidence_identity
                        if isinstance(termination, LifecycleActionTermination)
                        else None,
                    )
                raise error from exc
            with self._lock:
                if request.identity in self._cancelled:
                    raise ActionDagSchedulingError(
                        "action_dag.cancelled", "artifact request was cancelled"
                    )
            result = self.executor.admit(request, artifact)
            if (
                not isinstance(result, LifecycleActionDispatchOutcome)
                or result.request_identity != request.identity
            ):
                raise ActionDagSchedulingError(
                    "action_dag.outcome_mismatch",
                    "artifact admission returned another request's evidence",
                )
            with self._lock:
                if request.identity in self._cancelled:
                    raise ActionDagSchedulingError(
                        "action_dag.cancelled", "artifact request was cancelled"
                    )
            return result
        finally:
            with self._lock:
                users = self._users.get(key, {})
                users.pop(request.identity, None)
                if not users:
                    self._users.pop(key, None)

    @property
    def unresolved_productions(self):
        with self._lock:
            return tuple(
                (key, self._blocked_productions[key])
                for key in sorted(self._blocked_productions, key=lambda key: key.uri)
            )

    def resolve_production(
        self, key: ContentIdentity, termination: LifecycleActionTermination
    ) -> None:
        if not isinstance(termination, LifecycleActionTermination):
            raise ActionDagSchedulingError(
                "action_dag.termination_invalid",
                "production resolution requires trusted owner evidence",
            )
        with self._lock:
            request = self._blocked_productions.get(key)
            if (
                request is None
                or termination.request_identity != request.identity
                or termination.worker_identity != request.worker.worker_identity
                or termination.kind == "fenced-lease"
                and termination.fencing_policy_identity
                != self.loss_fencing_policy_identity
            ):
                raise ActionDagSchedulingError(
                    "action_dag.ownership_invalid",
                    "production resolution differs from held key/request/worker/policy",
                )
            self._blocked_productions.pop(key)
            self._artifacts.pop(key, None)

    def cancel(self, request: LifecycleActionDispatchRequest) -> None:
        producer = None
        with self._lock:
            self._cancelled.add(request.identity)
            for key, users in self._users.items():
                if request.identity in users and not any(
                    identity not in self._cancelled for identity in users
                ):
                    producer = self._producers.get(key)
                    break
        # A single waiter cannot terminate shared native work. Only cancellation
        # of every registered user reaches the actual producer's transport.
        if producer is not None:
            self.executor.cancel(producer)
