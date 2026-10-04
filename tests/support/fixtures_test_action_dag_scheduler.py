"""Shared test fixtures extracted from test_action_dag_scheduler."""

from __future__ import annotations

import threading

from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchOutcome,
    LifecycleActionWorker,
)
from literate_ai.contracts.identity import canonical_identity


def _identity(label: str):
    return canonical_identity({"test": label})


def _worker(worker_id: str, *, slots: int = 1) -> LifecycleActionWorker:
    return LifecycleActionWorker(
        worker_id,
        _identity(f"worker-{worker_id}"),
        _identity(f"catalog-{worker_id}"),
        _identity(f"observation-{worker_id}"),
        slots,
    )


class _RecordingDispatcher:
    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.failures = {} if failures is None else failures
        self.requests = []
        self.cancelled = []
        self.lock = threading.Lock()

    def dispatch(self, request):
        with self.lock:
            self.requests.append(request)
        failure = self.failures.get(request.action.action_id)
        return LifecycleActionDispatchOutcome(
            request.identity,
            None if failure else _identity(f"result-{request.action.action_id}"),
            failure,
        )

    def cancel(self, request):
        with self.lock:
            self.cancelled.append(request)
