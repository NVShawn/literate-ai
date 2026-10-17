"""Rehashed LINK results cannot change accepted manifests or dependency custody."""

import unittest

import tests.support.fixtures_test_action_link_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_link_result import LinkWorkerResult


class LinkWorkerResultTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.LinkWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.raw = f.value.to_bytes()
        self.identity = record_identity(self.raw)
        self.result = LinkWorkerResult.expected(self.raw, self.identity, f.deadline)

    def admit(self, raw=None, deadline=None):
        raw = self.result.to_bytes() if raw is None else raw
        return LinkWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=self.raw,
            input_identity=self.identity,
            deadline=deadline or self.fixture.deadline,
        )
