"""Coalesce immutable payload production while retaining fresh request admission."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Protocol

from literate_ai.application.action_dag_scheduler import (
    ActionDagSchedulingError,
    LifecycleActionDispatchOutcome,
    LifecycleActionDispatchRequest,
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

    def __init__(self, executor: ActionArtifactExecutor) -> None:
        self.executor = executor
        self._lock = threading.Lock()
        self._artifacts: dict[ContentIdentity, Future[ContentIdentity]] = {}

    def dispatch(
        self, request: LifecycleActionDispatchRequest
    ) -> LifecycleActionDispatchOutcome:
        key = self.executor.action_key(request)
        if not isinstance(key, ContentIdentity):
            raise ActionDagSchedulingError(
                "action_dag.key_invalid", "artifact executor returned an untyped key"
            )
        with self._lock:
            future = self._artifacts.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._artifacts[key] = future
        assert future is not None
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
                with self._lock:
                    # Waiters keep the failed Future. Only a subsequent explicit
                    # dispatch may retry; no successful outcome is synthesized.
                    self._artifacts.pop(key, None)
                raise
            else:
                future.set_result(artifact)
        result = self.executor.admit(request, future.result())
        if (
            not isinstance(result, LifecycleActionDispatchOutcome)
            or result.request_identity != request.identity
        ):
            raise ActionDagSchedulingError(
                "action_dag.outcome_mismatch",
                "artifact admission returned another request's evidence",
            )
        return result

    def cancel(self, request: LifecycleActionDispatchRequest) -> None:
        # Cancellation must not kill a producer shared by another request. The
        # executor owns cancellation/reference custody for its native transport.
        self.executor.cancel(request)
