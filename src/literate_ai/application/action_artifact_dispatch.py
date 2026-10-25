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
                    with self._lock:
                        self._artifacts.pop(key, None)
                    raise
                else:
                    future.set_result(artifact)
                finally:
                    with self._lock:
                        if self._producers.get(key) == request:
                            self._producers.pop(key, None)
            artifact = future.result()
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
