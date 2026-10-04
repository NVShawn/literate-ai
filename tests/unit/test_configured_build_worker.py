"""Configured receivers bind tools before fetch and clean supervised job custody."""

import io
import os
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.action_worker import main
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_dispatch_wire import (
    ActionWireError,
    decode_action_response,
    encode_action_request,
)
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from tests.support import fixtures_test_action_build_source as source_fixture
from tests.support.fixtures_test_action_build_action import build_request
from tests.support.fixtures_test_action_build_record import build_worker_input


class ConfiguredBuildWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.ActionBuildSourceTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.value = build_worker_input(self.fixture)
        ports = self.fixture.ports
        self.binding = next(iter(ports.tool_bindings.values()))
        contract = replace(
            self.value.inputs.contract,
            build_system_toolchain_identity=self.binding.toolchain_identity,
            language_runtime_identity=self.binding.toolchain_identity,
        )
        ports.contracts[contract.component_revision.uri] = contract
        intent = ports.create(
            self.value.execution_plan,
            self.value.generation_plan,
            self.value.candidate,
            (),
            (),
        )
        auth = ports.authorize(
            intent,
            ports.index(
                contract.component_revision, self.value.candidate.tree_identity
            ),
        )
        inputs = ports.plan_finalization_inputs(intent, auth)
        self.value = replace(self.value, inputs=inputs, plan=inputs.finalize())
        self.request, self.records = build_request(self.value, self.fixture.deadline)
        self.launcher = LocalComponentToolBinding(
            sys.executable, ("-c", "raise SystemExit(2)")
        )

    def execute(self, *, launcher=None, bindings=None, **changes):
        worker = ConfiguredBuildWorker(
            launcher or self.launcher,
            (self.binding,) if bindings is None else bindings,
            environment=dict(os.environ),
        )
        arguments = dict(
            request=self.request,
            deadline=self.fixture.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
            cas=self.fixture.cas,
            workspace_root=self.fixture.workspace,
            blob_source=self.fixture.controller_cas.get_bytes,
        )
        arguments.update(changes)
        return worker.execute(**arguments)

    def test_unknown_tool_is_refused_before_fetch_or_job_allocation(self):
        fetch = Mock(side_effect=AssertionError("unexpected source fetch"))
        other = LocalComponentToolBinding(sys.executable, ("-I",))
        with self.assertRaises(ActionWireError):
            self.execute(bindings=(other,), blob_source=fetch)
        fetch.assert_not_called()
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_profile_is_stable_across_transport_modes_but_not_build_settings(self):
        def configured(environment):
            return ConfiguredBuildWorker(
                self.launcher, (self.binding,), environment=environment
            )

        baseline = configured({"BUILD_SETTING": "one"})
        for marker in ("LITAI_DISPATCH_PROTOCOL", "litai_dispatch_protocol"):
            for protocol in ("capabilities", "hardware", "tools", "build"):
                with self.subTest(marker=marker, protocol=protocol):
                    worker = configured({"BUILD_SETTING": "one", marker: protocol})
                    self.assertEqual(worker.identity, baseline.identity)
                    self.assertEqual(dict(worker.environment), {"BUILD_SETTING": "one"})
        self.assertNotEqual(
            configured({"BUILD_SETTING": "two"}).identity, baseline.identity
        )
        with self.assertRaises(ValueError):
            configured({"LITAI_DISPATCH_PROTOCOL": None})

    def test_changed_standard_observations_refuse_before_fetch_or_job_allocation(self):
        observation = SimpleNamespace(
            command=self.binding.command,
            identity=self.binding.toolchain_identity.uri,
            environment=self.binding.environment,
            version=sys.version,
            version_info=tuple(sys.version_info[:3]),
            require_unchanged=self.binding.require_unchanged,
        )
        worker = ConfiguredBuildWorker(
            self.launcher,
            (self.binding,),
            environment=dict(os.environ),
            standard_tools={"python": observation},
        )
        observation.version += " changed"
        fetch = Mock(
            side_effect=AssertionError("source fetched before observation check")
        )
        with self.assertRaises(ActionWireError) as refused:
            worker.execute(
                self.request,
                self.fixture.deadline,
                self.records,
                expected_worker_identity=self.request.worker.worker_identity,
                cas=self.fixture.cas,
                workspace_root=self.fixture.workspace,
                blob_source=fetch,
            )
        self.assertEqual(refused.exception.code, "action_tools.observation_changed")
        fetch.assert_not_called()
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_failed_or_cancelled_child_cleans_readonly_job_files(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                marker = self.fixture.root / ("cancelled" if cancel else "failed")
                code = (
                    "from pathlib import Path; import time; "
                    "p=Path('output'); p.write_bytes(b'partial'); p.chmod(0o444); "
                    f"Path({str(marker)!r}).touch(); "
                    + ("time.sleep(60)" if cancel else "raise SystemExit(2)")
                )
                with self.assertRaises(ActionWireError):
                    self.execute(
                        launcher=LocalComponentToolBinding(
                            sys.executable, ("-c", code)
                        ),
                        cancelled=marker.exists if cancel else lambda: False,
                    )
                self.assertTrue(marker.exists())
                self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_corrupt_source_refuses_before_allocating_job(self):
        with self.assertRaises(ActionWireError):
            self.execute(blob_source=lambda reference: b"corrupt")
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_default_action_receiver_refuses_unconfigured_build(self):
        output = io.BytesIO()
        wire = encode_action_request(self.request, self.fixture.deadline, self.records)
        worker_identity = self.request.worker.worker_identity.uri
        with (
            patch(
                "literate_ai.action_worker.sys.stdin",
                SimpleNamespace(buffer=io.BytesIO(wire)),
            ),
            patch(
                "literate_ai.action_worker.sys.stdout", SimpleNamespace(buffer=output)
            ),
            patch.dict(
                os.environ,
                {"LITAI_ACTION_WORKER_IDENTITY": worker_identity},
            ),
        ):
            status = main(
                [
                    "--cas",
                    str(self.fixture.cas.root),
                    "--workspace",
                    str(self.fixture.workspace),
                ]
            )
        self.assertEqual(status, 0)
        outcome, result = decode_action_response(output.getvalue(), self.request)
        self.assertEqual(outcome.failure_code, "action_build.not_configured")
        self.assertIsNone(result)
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
