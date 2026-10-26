"""Held-reservation resolution of attested FINALIZE grant refusals."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_slots import CommandActionSlots
from literate_ai.adapters.command_finalizer import CommandProjectFinalizer
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionTermination,
)
from literate_ai.storage import FileSystemCAS
from tests.e2e.test_action_dag_scheduler import _identity, _node, _worker


class FinalizeGrantRefusalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.worker = _worker("alpha")
        self.deadline = ActionDispatchDeadline(
            datetime.now(UTC) + timedelta(seconds=30)
        )
        self.slots = CommandActionSlots((self.worker,), self.deadline)
        self.remembered = []
        self.finalizer = SimpleNamespace(
            indexer=SimpleNamespace(
                cas=FileSystemCAS(Path(temporary.name) / "cas"),
                slots=self.slots,
                remember_action_result=self.remembered.append,
            )
        )

    def request(self, slot):
        return LifecycleActionDispatchRequest(
            _identity("schedule"),
            _node("finalize"),
            self.worker,
            slot,
            (),
            self.deadline.identity,
        )

    def resolve(self, request, admitted):
        CommandProjectFinalizer._resolve_grant_refusal(
            self.finalizer, request, SimpleNamespace(admitted_failures=admitted)
        )

    def test_admitted_refusal_resolves_while_reservation_still_holds_slot(self):
        with self.slots.acquire() as (_worker_selected, slot):
            request = self.request(slot)
            self.slots.quarantine(request)
            response = b'{"fixture":"admitted grant refusal"}'
            self.resolve(request, {request.identity: response})
            self.assertEqual(self.slots.unresolved_ownership, ())
            # Resolution never frees a slot this reservation still holds.
            self.assertIsNone(self.slots.try_reserve(lambda worker, slot: None))
            self.assertEqual(
                self.finalizer.indexer.cas.get_bytes(
                    self.finalizer.indexer.cas.put_bytes(response)
                ),
                response,
            )
            self.assertEqual(len(self.remembered), 2)
        reusable = self.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reusable)
        reusable.release()

    def test_unverified_or_other_request_refusal_stays_unknown(self):
        with self.slots.acquire() as (_worker_selected, slot):
            request = self.request(slot)
            self.slots.quarantine(request)
            self.resolve(request, {})
            self.resolve(request, {_identity("other-request"): b"{}"})
            self.assertEqual(self.slots.unresolved_ownership, (request,))
        self.assertIsNone(self.slots.try_reserve(lambda worker, slot: None))
        with self.assertRaisesRegex(ActionWireError, "unresolved"):
            with self.slots.acquire():
                pass
        self.assertEqual(self.remembered, [])

    def test_external_resolution_after_release_still_frees_capacity(self):
        with self.slots.acquire() as (_worker_selected, slot):
            request = self.request(slot)
            self.slots.quarantine(request)
        self.slots.resolve_ownership(
            LifecycleActionTermination(
                request.identity,
                self.worker.worker_identity,
                _identity("owner-terminal"),
                "owned-terminal",
                0,
            )
        )
        reusable = self.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reusable)
        reusable.release()


if __name__ == "__main__":
    unittest.main()
