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
    encode_action_response,
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

    def refusal(self, request, code="action_finalize.grant_unavailable"):
        return {request.identity: encode_action_response(request, failure_code=code)}

    def test_admitted_refusal_resolves_while_reservation_still_holds_slot(self):
        with self.slots.acquire() as (_worker_selected, slot):
            request = self.request(slot)
            self.slots.quarantine(request)
            admitted = self.refusal(request)
            self.resolve(request, admitted)
            self.assertEqual(self.slots.unresolved_ownership, ())
            # Resolution never frees a slot this reservation still holds.
            self.assertIsNone(self.slots.try_reserve(lambda worker, slot: None))
            response = admitted[request.identity]
            cas = self.finalizer.indexer.cas
            self.assertEqual(cas.get_bytes(cas.put_bytes(response)), response)
            self.assertEqual(len(self.remembered), 2)
            self.assertIn(
                b'"refusal_phase":"pre-staging-admission"', self.remembered[1]
            )
        reusable = self.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reusable)
        reusable.release()

    def test_unverified_other_code_or_unheld_refusal_raises_and_stays_unknown(self):
        with self.slots.acquire() as (_worker_selected, slot):
            request = self.request(slot)
            self.slots.quarantine(request)
            for admitted in (
                {},
                {_identity("other-request"): b"{}"},
                self.refusal(request, "action_finalize.grant_lost"),
            ):
                with self.subTest(admitted=sorted(item.uri for item in admitted)):
                    with self.assertRaises(ActionWireError) as refused:
                        self.resolve(request, admitted)
                    self.assertEqual(
                        refused.exception.code, "action_finalize.refusal_unverified"
                    )
            self.assertEqual(self.slots.unresolved_ownership, (request,))
        # Once the reservation is released, self-resolution is refused.
        with self.assertRaises(ActionWireError) as refused:
            self.resolve(request, self.refusal(request))
        self.assertEqual(refused.exception.code, "action_slots.ownership_invalid")
        self.assertEqual(self.slots.unresolved_ownership, (request,))
        self.assertIsNone(self.slots.try_reserve(lambda worker, slot: None))
        with self.assertRaisesRegex(ActionWireError, "unresolved"):
            with self.slots.acquire():
                pass

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
