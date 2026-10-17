"""Invalid or revoked PACKAGE authority cannot instantiate a private adapter."""

import unittest
from unittest.mock import Mock

import tests.support.fixtures_test_action_package as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_package_worker import ConfiguredPackageWorker
from literate_ai.contracts import canonical_identity


class ConfiguredPackageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.PackageActionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.factory = Mock()
        self.guard = Mock()
        self.worker = ConfiguredPackageWorker(
            f.fixture.value.plan.packager_identity, self.factory, self.guard
        )

    def execute(self, **changes):
        f = self.fixture
        args = dict(
            expected_worker_identity=f.request.worker.worker_identity,
            cas=Mock(),
            admission_guard=lambda: None,
        )
        args.update(changes)
        return self.worker.execute(f.request, f.fixture.deadline, f.records, **args)

    def test_wrong_receiver_rejects_before_adapter_creation(self):
        with self.assertRaises(ActionWireError):
            self.execute(expected_worker_identity=canonical_identity("other-worker"))
        self.factory.assert_not_called()

    def test_revoked_private_configuration_rejects_before_adapter_creation(self):
        self.guard.side_effect = ActionWireError(
            "fixture.revoked", "configuration changed"
        )
        with self.assertRaises(ActionWireError) as error:
            self.execute()
        self.assertEqual(error.exception.code, "fixture.revoked")
        self.factory.assert_not_called()

    def test_cancelled_dispatch_rejects_before_adapter_creation(self):
        with self.assertRaises(ActionWireError) as error:
            self.execute(cancelled=lambda: True)
        self.assertEqual(error.exception.code, "action_package.cancelled")
        self.factory.assert_not_called()

    def test_wrong_packager_rejects_before_adapter_creation(self):
        self.worker = ConfiguredPackageWorker(
            canonical_identity("other-packager"), self.factory, self.guard
        )
        with self.assertRaises(ActionWireError) as error:
            self.execute()
        self.assertEqual(error.exception.code, "action_package.packager_mismatch")
        self.factory.assert_not_called()
