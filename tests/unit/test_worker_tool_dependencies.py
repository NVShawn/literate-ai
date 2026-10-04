"""Worker native graphs retain real library evidence and exact tool selection."""

import sys
import unittest
from unittest.mock import Mock, patch

from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.lifecycle import LocalComponentToolBinding

ROOT = "urn:literate-ai:component:worker-test"


class WorkerToolDependencyTests(unittest.TestCase):
    def setUp(self):
        self.binding = LocalComponentToolBinding(sys.executable)
        self.registry = WorkerToolchainRegistry((self.binding,))
        self.guard = Mock()

    def test_configured_worker_reobserves_and_refuses_changed_library(self):
        worker = ConfiguredBuildWorker(self.binding, (self.binding,), environment={})
        with patch(
            "literate_ai.adapters.worker_tool_dependencies.PortableHostDependencyObserver"
        ) as observer:
            observer.return_value.observe.return_value = HostDependencyObservation(
                ({"bom-ref": "library", "version": "one"},),
                ((ROOT, "library"),),
            )
            args = dict(root_ref=ROOT, require_current=self.guard)
            first = worker.observe_tool_dependencies(self.registry.identities, **args)
            worker.observe_tool_dependencies(
                self.registry.identities, expected_identity=first.identity, **args
            )
            observer.return_value.observe.return_value = HostDependencyObservation(
                ({"bom-ref": "library", "version": "two"},),
                ((ROOT, "library"),),
            )
            with self.assertRaises(ActionWireError) as caught:
                worker.observe_tool_dependencies(
                    self.registry.identities, expected_identity=first.identity, **args
                )
            self.assertEqual(caught.exception.code, "action_tools.dependencies_changed")
