"""Receiver transport metadata must not change private FINALIZE identity."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.action_capabilities import CAPABILITY_PROTOCOL
from literate_ai.adapters.action_dispatch_wire import (
    ACTION_WIRE_PROTOCOL,
    ActionWireError,
)
from literate_ai.adapters.action_finalize_parent import portable_child_environment
from literate_ai.adapters.action_finalize_worker import ConfiguredFinalizeWorker
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_identity


class ConfiguredFinalizeWorkerTests(unittest.TestCase):
    def worker(self, environment):
        profile = canonical_identity("private-finalize-test-runtime")
        return ConfiguredFinalizeWorker(
            LocalComponentToolBinding(sys.executable),
            environment=environment,
            runtime_identity=profile,
            observe_runtime=lambda: profile,
            require_execution_authority=lambda value: None,
            verify_package=lambda *args: None,
            verify_stages=lambda *args: None,
        )

    def test_discovery_and_execution_have_same_private_profile(self):
        plain = self.worker({"RUNTIME_MODE": "production"})
        for key in ("LITAI_DISPATCH_PROTOCOL", "litai_dispatch_protocol"):
            for protocol in (CAPABILITY_PROTOCOL, ACTION_WIRE_PROTOCOL):
                with self.subTest(key=key, protocol=protocol):
                    worker = self.worker({"RUNTIME_MODE": "production", key: protocol})
                    self.assertEqual(worker.identity, plain.identity)
                    self.assertEqual(dict(worker.environment), dict(plain.environment))

    def test_runtime_environment_remains_bound_and_owned(self):
        environment = {"RUNTIME_MODE": "production"}
        worker = self.worker(environment)
        identity = worker.identity
        environment["RUNTIME_MODE"] = "development"
        self.assertEqual(worker.identity, identity)
        self.assertNotEqual(self.worker(environment).identity, identity)
        with self.assertRaises(TypeError):
            worker.environment["RUNTIME_MODE"] = "development"

    def test_nontext_environment_is_refused_before_filtering(self):
        for environment in ({1: "value"}, {"LITAI_DISPATCH_PROTOCOL": None}):
            with self.subTest(environment=environment), self.assertRaises(TypeError):
                self.worker(environment)

    def test_parent_measures_the_environment_the_child_receives(self):
        # The bounded launcher forwards run-scoped diagnostics to the child.
        with mock.patch.dict(os.environ, {"LITAI_DEBUG": "1"}):
            measured = portable_child_environment({"SystemRoot": "C:/Windows"})
        self.assertEqual(measured.get("LITAI_DEBUG"), "1")
        key = "SYSTEMROOT" if os.name == "nt" else "SystemRoot"
        self.assertEqual(measured.get(key), "C:/Windows")

    def grant_refusal_code(self, refuse_on_call):
        calls = 0

        def authority(value):
            nonlocal calls
            calls += 1
            if calls >= refuse_on_call:
                raise ActionWireError(
                    "action_finalize.grant_unavailable", "fixture grant unreadable"
                )

        profile = canonical_identity("private-finalize-test-runtime")
        worker = ConfiguredFinalizeWorker(
            LocalComponentToolBinding(sys.executable),
            environment={"RUNTIME_MODE": "production"},
            runtime_identity=profile,
            observe_runtime=lambda: profile,
            require_execution_authority=authority,
            verify_package=lambda *args: None,
            verify_stages=lambda *args: None,
        )
        request = SimpleNamespace(
            input_record_identities=(canonical_identity("input"),),
            action=SimpleNamespace(payload_identity=canonical_identity("payload")),
        )
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root).resolve() / "workspace"
            workspace.mkdir(mode=0o700)
            with (
                mock.patch(
                    "literate_ai.adapters.action_finalize_worker.admit_finalize_action",
                    return_value=object(),
                ),
                self.assertRaises(ActionWireError) as refused,
            ):
                worker.execute(
                    request,
                    SimpleNamespace(remaining=lambda: 60.0),
                    {},
                    expected_worker_identity=canonical_identity("worker"),
                    cas=SimpleNamespace(root=Path(root).resolve() / "cas"),
                    workspace_root=workspace,
                    admission_guard=lambda: None,
                )
            # Nothing staged by this receiver survives either refusal.
            self.assertEqual(list(workspace.iterdir()), [])
        return refused.exception.code

    def test_only_the_pre_staging_grant_refusal_is_grant_unavailable(self):
        self.assertEqual(
            self.grant_refusal_code(1), "action_finalize.grant_unavailable"
        )
        # A grant that becomes unreadable at any later check is a distinct,
        # post-admission refusal that never authorizes owner self-resolution.
        self.assertEqual(self.grant_refusal_code(2), "action_finalize.grant_lost")
