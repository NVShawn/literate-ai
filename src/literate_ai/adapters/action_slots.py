"""Shared bounded worker capacity for independently dispatched lifecycle phases."""

from __future__ import annotations

import threading
from contextlib import contextmanager

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionTermination,
)


class ReservedAction:
    def __init__(self, owner, worker, slot, operation):
        self.owner, self.worker, self.slot = owner, worker, slot
        self.operation = operation
        self.started = self.finished = self.released = False

    def run(self):
        with self.owner.condition:
            if self.started or self.released:
                raise ValueError("action reservation is no longer executable")
            self.started = True
        try:
            return self.operation(self.worker, self.slot)
        except BaseException as exc:
            request = getattr(exc, "unknown_ownership_request", None)
            if request is not None:
                self.owner.quarantine(request)
            raise
        finally:
            with self.owner.condition:
                self.finished = True
            self.release()

    def release(self):
        with self.owner.condition:
            if self.started and not self.finished:
                return
            if not self.released:
                self.released = True
                key = (self.worker.worker_id, self.slot)
                self.owner._held.discard(key)
                if not any(
                    (request.worker.worker_id, request.slot) == key
                    for request in self.owner._unknown.values()
                ):
                    self.owner._occupied.remove(key)
                self.owner.condition.notify_all()


class CommandActionSlots:
    def __init__(self, workers, deadline, *, loss_fencing_policy_identity=None):
        self.workers = tuple(workers)
        self.deadline = deadline
        self.condition = threading.Condition()
        self._occupied = set()
        self._unknown = {}
        # Slots a live reservation still holds; resolution never frees these.
        self._held = set()
        self.loss_fencing_policy_identity = loss_fencing_policy_identity
        self.started = False
        if (
            not self.workers
            or not 1 <= sum(worker.slots for worker in self.workers) <= 256
        ):
            raise ActionWireError(
                "action_slots.capacity_invalid", "worker capacity is invalid"
            )

    @property
    def unresolved_ownership(self):
        with self.condition:
            return tuple(
                self._unknown[key]
                for key in sorted(self._unknown, key=lambda key: key.uri)
            )

    def quarantine(self, request):
        with self.condition:
            if (
                not isinstance(request, LifecycleActionDispatchRequest)
                or request.worker not in self.workers
                or (request.worker.worker_id, request.slot) not in self._occupied
            ):
                raise ActionWireError(
                    "action_slots.ownership_invalid",
                    "unknown actuation differs from its reserved worker/slot",
                )
            self._unknown[request.identity] = request
            self.condition.notify_all()

    def resolve_ownership(self, termination):
        with self.condition:
            if not isinstance(termination, LifecycleActionTermination):
                raise ActionWireError(
                    "action_slots.ownership_invalid",
                    "trusted terminal/fence evidence is required",
                )
            request = self._unknown.get(termination.request_identity)
            if (
                request is None
                or request.worker.worker_identity != termination.worker_identity
                or termination.kind == "fenced-lease"
                and termination.fencing_policy_identity
                != self.loss_fencing_policy_identity
            ):
                raise ActionWireError(
                    "action_slots.ownership_invalid",
                    "resolution differs from retained request/worker/fencing policy",
                )
            self._unknown.pop(request.identity)
            key = (request.worker.worker_id, request.slot)
            if key not in self._held:
                self._occupied.discard(key)
            self.condition.notify_all()

    def try_reserve(self, operation, *, eligible_worker_ids=None):
        eligible = (
            {worker.worker_id for worker in self.workers}
            if eligible_worker_ids is None
            else set(eligible_worker_ids)
        )
        if not eligible or not eligible <= {
            worker.worker_id for worker in self.workers
        }:
            raise ActionWireError(
                "action_slots.worker_invalid", "phase has no admitted workers"
            )
        with self.condition:
            self.started = True
            self.deadline.remaining()
            for worker in self.workers:
                if worker.worker_id not in eligible or any(
                    request.worker.worker_id == worker.worker_id
                    for request in self._unknown.values()
                ):
                    continue
                for slot in range(worker.slots):
                    key = (worker.worker_id, slot)
                    if key not in self._occupied:
                        self._occupied.add(key)
                        self._held.add(key)
                        return ReservedAction(self, worker, slot, operation)
            return None

    @contextmanager
    def acquire(self, *, eligible_worker_ids=None):
        with self.condition:
            while True:
                reservation = self.try_reserve(
                    lambda worker, slot: None, eligible_worker_ids=eligible_worker_ids
                )
                if reservation is not None:
                    break
                eligible = (
                    {worker.worker_id for worker in self.workers}
                    if eligible_worker_ids is None
                    else set(eligible_worker_ids)
                )
                if eligible <= {
                    request.worker.worker_id for request in self._unknown.values()
                }:
                    raise ActionWireError(
                        "action_slots.ownership_unknown",
                        "eligible workers still own unresolved actions",
                    )
                self.condition.wait(timeout=min(0.1, self.deadline.remaining()))
        try:
            yield reservation.worker, reservation.slot
        except BaseException as exc:
            request = getattr(exc, "unknown_ownership_request", None)
            if request is not None:
                self.quarantine(request)
            raise
        finally:
            reservation.release()
