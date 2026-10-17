"""FINALIZE descriptors cannot substitute lock, component plans or package intent."""

import unittest

import tests.support.fixtures_test_action_package_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.contracts import canonical_identity


class FinalizeWorkerInputTests(unittest.TestCase):
    def setUp(self):
        f = fixture_module.PackageWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.deadline = f.deadline
        self.package = f.fixture.result.root_integration.package_result
        self.value = FinalizeWorkerInput(
            f.fixture.fixture.lock,
            f.fixture.result.project_build_plan,
            f.value,
            canonical_identity("package-result"),
        )

    def admit(self, value=None, raw=None):
        raw = (value or self.value).to_bytes() if raw is None else raw
        return FinalizeWorkerInput.admit(raw, record_identity(raw), self.deadline)
