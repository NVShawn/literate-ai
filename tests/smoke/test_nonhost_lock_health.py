"""Verify real committed non-host locks without rewriting target or policy."""

import json
import unittest

from literate_ai.adapters.component_lock_application import current_component_lock
from literate_ai.adapters.project_lock_health import observe_project_locks
from tests.smoke import test_installed_lock_health_smoke as fixtures
from tests.smoke.test_project_verify import invoke, tree_snapshot


class NonhostLockHealthTests(unittest.TestCase):
    setUp = fixtures.InstalledLockHealthSmokeTests.setUp

    def lock(self):
        status, result = invoke("lock", str(self.component), "--target", "native-test")
        self.assertEqual(status, 0, result)

    def test_nonhost_verification_preserves_current_target_and_files(self):
        self.lock()
        before = tree_snapshot(self.root)
        current = current_component_lock(self.component)
        self.assertEqual(current.target_name, "native-test")
        status, report = invoke("verify", str(self.root), "--gate", "locks")
        self.assertEqual(status, 0, report)
        self.assertEqual(report["result"]["counts"]["pass"], 1)
        observed = observe_project_locks(self.root)
        self.assertEqual(observed.gate.state, "pass")
        self.assertEqual(observed.components[0]["state"], "current")
        status, report = invoke("status", "--project", str(self.root))
        self.assertEqual(status, 0, report)
        self.assertEqual(report["result"]["project"]["locks"]["state"], "current")
        self.assertEqual(
            current_component_lock(self.component).identity, current.identity
        )
        self.assertEqual(tree_snapshot(self.root), before)

    def test_changed_project_flavor_policy_remains_stale(self):
        self.lock()
        definition = json.loads(self.configuration.read_text())
        definition["component_flavor_selectors"]["samples/hello-component"] = [
            "+macos",
            "+python",
            "+bazel",
        ]
        self.configuration.write_text(json.dumps(definition))
        observed = observe_project_locks(self.root)
        self.assertEqual(observed.gate.state, "fail")

    def test_missing_target_audit_remains_error(self):
        self.lock()
        (self.component / "component.resolution-audit.native-test.json").unlink()
        self.assertEqual(observe_project_locks(self.root).gate.state, "fail")

    def test_changed_authoring_contract_remains_error(self):
        self.lock()
        path = self.component / "component.md"
        path.write_text(path.read_text() + "\nChanged contract\n")
        self.assertEqual(observe_project_locks(self.root).gate.state, "fail")

    def test_changed_selected_flavor_remains_error(self):
        self.lock()
        path = self.root / "flavors/lang-python/flavor.md"
        path.write_text(path.read_text() + "\nChanged selected policy\n")
        self.assertEqual(observe_project_locks(self.root).gate.state, "fail")

    def test_substituted_target_does_not_find_another_audit(self):
        self.lock()
        path = self.component / "component.lock.json"
        document = json.loads(path.read_text())
        document["target_name"] = "host"
        path.write_text(json.dumps(document))
        self.assertEqual(observe_project_locks(self.root).gate.state, "fail")


if __name__ == "__main__":
    unittest.main()
