"""Explicit protocol opt-in and real bounded command capability probes."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime

from literate_ai.adapters.action_capabilities import (
    probe_command_action_capabilities,
    receiver_code_identity,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.support import fixtures_test_action_source_index as source_fixture
from tests.support.action_deadline import ACTION_TEST_DEADLINE


class ActionCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceIndexActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.worker = replace(
            self.fixture.worker, action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL
        )
        # These scenarios perform multiple probes and sometimes an index dispatch.
        # Each production probe retains its own cap inside this scenario budget.
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        self.fixture.deadline = self.deadline
        self.fixture.request = replace(
            self.fixture.request, deadline_identity=self.deadline.identity
        )

    def probe(self, worker=None, **kwargs):
        worker = self.worker if worker is None else worker
        return probe_command_action_capabilities(
            worker,
            self.deadline,
            cwd=self.fixture.root,
            environment={**os.environ, "INDEX_WORKER_IDENTITY": worker.identity.uri},
            **kwargs,
        )

    def request(self, nonce="a" * 32):
        return canonical_json_bytes(
            {
                "schema": "literate-ai/action-capability-request@1",
                "worker_identity": self.worker.identity.uri,
                "nonce": nonce,
                "deadline": self.deadline.to_dict(),
            }
        )

    def test_real_probe_binds_runtime_worker_and_read_only_capabilities(self):
        first = self.probe()
        second = self.probe()
        self.assertEqual(first.worker_identity, self.worker.identity)
        self.assertEqual(first.receiver_identity, receiver_code_identity())
        self.assertEqual(
            first.actions,
            (
                LifecycleActionKind.AUTHORIZE,
                LifecycleActionKind.BUILD_INTENT,
                LifecycleActionKind.INDEX,
                LifecycleActionKind.PLAN,
            ),
        )
        self.assertEqual(first.source_handoff, ("filesystem-cas",))
        self.assertEqual(first.capability_identity, second.capability_identity)
        self.assertNotEqual(first.request_identity, second.request_identity)
        self.assertNotEqual(first.identity, second.identity)
        first.require_current(self.worker, receiver_code_identity())
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_private_worker_identity_mismatch_refuses_real_probe(self):
        with self.assertRaises(ActionWireError) as raised:
            probe_command_action_capabilities(
                self.worker,
                self.deadline,
                cwd=self.fixture.root,
                environment={
                    **os.environ,
                    "INDEX_WORKER_IDENTITY": canonical_identity("other").uri,
                },
            )
        self.assertEqual(raised.exception.code, "action_capability.probe_failed")
