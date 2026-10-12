"""Private startup failures restore controls, ownership and stage admission."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs
from literate_ai.adapters.action_finalize_startup import PortableFinalizeRuntimeFactory
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
)
from literate_ai.contracts import canonical_identity


class FinalizeStartupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.workspace = self.root / "job"
        self.workspace.mkdir()
        self.tools = (LocalComponentToolBinding(sys.executable),)

    def factory(self, **changes):
        args = dict(
            workspace_root=self.workspace,
            contracts=(),
            tool_bindings=self.tools,
            oracle=None,
            startup=WorkerToolchainRegistry(self.tools),
            grant_path=self.root / "grant.json",
            runtime_identity=canonical_identity("runtime"),
            admission_guard=lambda: None,
        )
        return PortableFinalizeRuntimeFactory(**(args | changes))

    def test_grant_cannot_be_read_from_generated_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside child workspace"):
            self.factory(grant_path=self.workspace / "grant.json")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_failed_runtime_restores_environment_cleans_owned_output_and_unlocks(self):
        factory = self.factory()
        # Invalid private tool/contract composition fails before reading any grant.
        prepared = FinalizeRuntimeInputs(
            None, None, None, LocalSourceTreeRegistry(), ()
        )
        preserved = self.workspace / "keep"
        preserved.write_text("other work")
        controls = {
            "LITAI_FINALIZE_CAS": "transport-cas",
            "litai_finalize_workspace": "transport-workspace",
        }
        with patch.dict(os.environ, controls):
            before = dict(os.environ)
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "every and only locked"):
                    with factory(prepared):
                        self.fail("invalid runtime opened")
                self.assertEqual(dict(os.environ), before)
                self.assertEqual(list(self.workspace.iterdir()), [preserved])
                with self.assertRaises(ActionWireError) as error:
                    factory.require_stage("root-integration-test", prepared)
                self.assertEqual(
                    error.exception.code, "action_finalize.authority_mismatch"
                )
        self.assertEqual(preserved.read_text(), "other work")

    def test_replaced_workspace_refuses_without_touching_replacement(self):
        factory = self.factory()
        self.workspace.rename(self.root / "old")
        self.workspace.mkdir()
        prepared = FinalizeRuntimeInputs(
            None, None, None, LocalSourceTreeRegistry(), ()
        )
        with self.assertRaises(ActionWireError) as error:
            with factory(prepared):
                self.fail("replaced workspace opened")
        self.assertEqual(error.exception.code, "action_finalize.workspace_invalid")
        self.assertEqual(list(self.workspace.iterdir()), [])
