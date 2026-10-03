"""Worker tool selections preserve launcher/runtime observation custody."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


class WorkerToolchainRegistryTests(unittest.TestCase):
    def test_actual_observed_python_can_be_selected_from_portable_identity(self):
        observed = discover_python_toolchain(pinned_command=(sys.executable,))
        binding = LocalComponentToolBinding.from_observed_toolchain(observed)
        registry = WorkerToolchainRegistry((binding,))
        portable = ContentIdentity.parse_uri(observed.identity)
        self.assertEqual(registry.select((portable,)), (binding,))
        self.assertEqual(registry.identities, (portable,))
        self.assertIsInstance(registry.identity, ContentIdentity)

    def test_worker_resolves_its_own_path_and_refuses_launcher_drift(self):
        with tempfile.TemporaryDirectory() as scratch:
            first = Path(scratch) / "controller"
            second = Path(scratch) / "worker"
            first.write_bytes(b"tool-v1")
            second.write_bytes(b"tool-v1")
            controller = LocalComponentToolBinding(str(first))
            worker = LocalComponentToolBinding(str(second.resolve()))
            identity = controller.toolchain_identity
            registry = WorkerToolchainRegistry((worker,))
            first.unlink()
            self.assertEqual(
                registry.select((identity,))[0].executable, str(second.resolve())
            )
            second.write_bytes(b"tool-v2")
            with self.assertRaisesRegex(ActionWireError, "no longer current"):
                registry.select((identity,))

    def test_opaque_alias_requires_guard_and_guard_is_rechecked(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "tool"
            path.write_bytes(b"launcher")
            identity = canonical_identity("observed-runtime")
            alias = LocalComponentToolBinding(str(path), authority_identity=identity)
            with self.assertRaisesRegex(ActionWireError, "observation guard"):
                WorkerToolchainRegistry((alias,))
            guard = Mock()
            binding = LocalComponentToolBinding(
                str(path), authority_identity=identity, _authority_guard=guard
            )
            registry = WorkerToolchainRegistry((binding,))
            guard.side_effect = RuntimeError("private-path-secret")
            with self.assertRaises(ActionWireError) as caught:
                registry.select((identity,))
            self.assertEqual(caught.exception.code, "action_tools.changed")
            self.assertNotIn("secret", str(caught.exception))

    def test_duplicate_missing_and_noncanonical_selections_refuse(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "tool"
            path.write_bytes(b"launcher")
            binding = LocalComponentToolBinding(str(path))
            identity = binding.toolchain_identity
            with self.assertRaisesRegex(ActionWireError, "duplicate"):
                WorkerToolchainRegistry((binding, binding))
            registry = WorkerToolchainRegistry((binding,))
            for identities in (
                (identity, identity),
                (canonical_identity("missing"),),
                (str(path),),
            ):
                with self.assertRaises(ActionWireError):
                    registry.select(identities)
            with self.assertRaises(TypeError):
                registry._bindings[identity] = binding

    def test_argument_and_environment_changes_are_different_tools(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "tool"
            path.write_bytes(b"launcher")
            first = LocalComponentToolBinding(
                str(path),
                arguments=("--first",),
                environment=(("PRIVATE", "hidden-value"),),
            )
            second = LocalComponentToolBinding(str(path), arguments=("--second",))
            registry = WorkerToolchainRegistry((second,))
            with self.assertRaisesRegex(ActionWireError, "exact requested toolchain"):
                registry.select((first.toolchain_identity,))
            self.assertNotIn("hidden-value", str(registry.identities))
            self.assertNotIn(str(path), str(registry.identities))
