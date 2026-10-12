"""LINK requires exact accepted exports and only its production DAG predecessors."""

import unittest

import tests.support.fixtures_test_action_accept_result as fixture_module
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_link_record import LinkWorkerInput


class LinkWorkerInputTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.AcceptWorkerResultTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.deadline = f.worker.deadline
        self.value = LinkWorkerInput(f.value, f.result)

    def admit(self, raw=None):
        raw = self.value.to_bytes() if raw is None else raw
        return LinkWorkerInput.admit(raw, record_identity(raw), self.deadline)
