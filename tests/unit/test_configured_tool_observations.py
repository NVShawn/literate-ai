"""Only registered live worker commands may supply Standard tool observations."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.configured_tool_observations import ConfiguredToolObservations
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_identity


class ConfiguredToolObservationTests(unittest.TestCase):
    def setUp(self):
        self.tool = discover_python_toolchain(pinned_command=(sys.executable,))
        self.binding = LocalComponentToolBinding.from_observed_toolchain(self.tool)
        self.registry = WorkerToolchainRegistry((self.binding,))

    def mutable_observation(self, **changes):
        return SimpleNamespace(
            **{
                "command": self.tool.command,
                "identity": self.tool.identity,
                "environment": (),
                "version": self.tool.version,
                "version_info": self.tool.version_info,
                "require_unchanged": self.tool.require_unchanged,
                **changes,
            }
        )

    def test_actual_python_registry_and_inventory_are_part_of_profile(self):
        worker = ConfiguredBuildWorker(
            self.binding,
            (self.binding,),
            environment={},
            standard_tools={"python": self.tool},
        )
        guard = Mock()
        observed = worker.observe_tools(require_current=guard)
        self.assertEqual(observed.tools[0].toolchain_identity.uri, self.tool.identity)
        self.assertEqual(observed.tools[0].command, self.binding.command)
        self.assertIn(observed.platform, ("linux", "macos", "windows"))
        guard.assert_called()
        plain = ConfiguredBuildWorker(self.binding, (self.binding,), environment={})
        self.assertNotEqual(worker.identity, plain.identity)
        self.assertEqual(worker.identity, worker.identity)
        with self.assertRaises(ActionWireError) as refused:
            plain.observe_tools(require_current=guard)
        self.assertEqual(refused.exception.code, "action_tools.not_configured")

    def test_same_identity_with_different_command_or_environment_refuses(self):
        for changes in (
            {"command": ("/other/python",)},
            {"environment": (("LIB_PATH", "other"),)},
            {"identity": canonical_identity("other").uri},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ActionWireError) as refused,
            ):
                ConfiguredToolObservations(
                    self.registry, {"python": self.mutable_observation(**changes)}
                )
            self.assertEqual(
                refused.exception.code, "action_tools.observation_mismatch"
            )

    def test_missing_registered_tool_cannot_be_omitted(self):
        other = LocalComponentToolBinding(
            sys.executable,
            ("-I",),
            authority_identity=canonical_identity("other"),
            _authority_guard=lambda: None,
        )
        with self.assertRaises(ActionWireError) as refused:
            ConfiguredToolObservations(
                WorkerToolchainRegistry((self.binding, other)), {"python": self.tool}
            )
        self.assertEqual(refused.exception.code, "action_tools.observation_mismatch")

    def test_metadata_drift_is_refused_even_when_tool_guard_remains_current(self):
        tool = self.mutable_observation()
        worker = ConfiguredBuildWorker(
            self.binding,
            (self.binding,),
            environment={},
            standard_tools={"python": tool},
        )
        tool.version += " changed"
        for operation in (
            lambda: worker.identity,
            lambda: worker.observe_tools(require_current=lambda: None),
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaises(ActionWireError) as refused,
            ):
                operation()
            self.assertEqual(refused.exception.code, "action_tools.observation_changed")

    def test_private_mapping_is_copied_and_expired_transport_guard_refuses(self):
        source = {"python": self.tool}
        configured = ConfiguredToolObservations(self.registry, source)
        source.clear()
        self.assertTrue(configured.capture(require_current=lambda: None).tools)
        with self.assertRaisesRegex(RuntimeError, "expired"):
            configured.capture(
                require_current=Mock(side_effect=RuntimeError("expired"))
            )
