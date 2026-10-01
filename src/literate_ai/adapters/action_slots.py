"""Shared bounded worker capacity for independently dispatched lifecycle phases."""

from __future__ import annotations

import threading
from contextlib import contextmanager

from literate_ai.adapters.action_dispatch_wire import ActionWireError


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
                self.owner._occupied.remove((self.worker.worker_id, self.slot))
                self.owner.condition.notify_all()


class CommandActionSlots:
    def __init__(self, workers, deadline):
        self.workers = tuple(workers)
        self.deadline = deadline
        self.condition = threading.Condition()
        self._occupied = set()
        self.started = False
        if (
            not self.workers
            or not 1 <= sum(worker.slots for worker in self.workers) <= 256
        ):
            raise ActionWireError(
                "action_slots.capacity_invalid", "worker capacity is invalid"
            )

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
                if worker.worker_id not in eligible:
                    continue
                for slot in range(worker.slots):
                    key = (worker.worker_id, slot)
                    if key not in self._occupied:
                        self._occupied.add(key)
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
                self.condition.wait(timeout=min(0.1, self.deadline.remaining()))
        try:
            yield reservation.worker, reservation.slot
        finally:
            reservation.release()
