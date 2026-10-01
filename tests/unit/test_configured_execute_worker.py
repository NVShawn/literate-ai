"""Configured EXECUTE workers reject stale inputs before allocating owned jobs."""

import io
import json
import os
import shutil
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.action_worker import main
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_dispatch_wire import (
    ActionWireError,
    decode_action_response,
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.action_execute_record import required_execute_toolchains
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import ComponentCommandPhase, canonical_identity
from tests.unit import test_action_execute_execution as execution_fixture
from tests.unit.test_action_execute_action import make_execute_request
from tests.unit.test_component_command_contracts import (
    contract,
    entrypoint_contract,
    identity,
)


class ConfiguredExecuteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = execution_fixture.ActionExecuteExecutionTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.workspace = f.fixture.root / "configured-execute"
        self.workspace.mkdir()
        self.request, self.records = make_execute_request(f.value, f.deadline)
        self.binding = next(iter(f.ports.tool_bindings.values()))
        self.launcher = LocalComponentToolBinding(
            sys.executable, ("-c", "raise SystemExit(2)")
        )

    def worker(self, *, launcher=None, bindings=None, environment=None):
        return ConfiguredExecuteWorker(
            launcher or self.launcher,
            (self.binding,) if bindings is None else bindings,
            environment=dict(os.environ) if environment is None else environment,
        )

    def execute(self, worker=None, **changes):
        f = self.fixture
        arguments = dict(
            request=self.request,
            deadline=f.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
            cas=f.cas,
            workspace_root=self.workspace,
            blob_source=f.source_cas.get_bytes,
        )
        arguments.update(changes)
        return (worker or self.worker()).execute(**arguments)

    def test_execute_profile_is_distinct_and_requires_only_execute_runtime(self):
        worker = self.worker(environment={})
        build = ConfiguredBuildWorker(self.launcher, (self.binding,), environment={})
        self.assertNotEqual(worker.identity, build.identity)
        self.assertEqual(
            required_execute_toolchains(self.fixture.value.build_input.inputs),
            (self.binding.toolchain_identity,),
        )

    def test_all_entrypoint_execute_runtimes_are_required(self):
        entries = []
        expected = {identity("runner-a"), identity("runner-b")}
        for name, runner in zip(
            ("api", "frontend"),
            sorted(expected, key=lambda item: item.uri),
            strict=True,
        ):
            entry = entrypoint_contract(name)
            entries.append(
                replace(
                    entry,
                    tool_bindings=tuple(
                        replace(binding, toolchain_identity=runner)
                        if binding.phase is ComponentCommandPhase.EXECUTE
                        else binding
                        for binding in entry.tool_bindings
                    ),
                )
            )
        inputs = replace(
            self.fixture.value.build_input.inputs,
            contract=replace(contract(), entrypoint_contracts=tuple(entries)),
        )
        self.assertEqual(set(required_execute_toolchains(inputs)), expected)

    def test_missing_execute_runtime_refuses_before_fetch_and_allocation(self):
        fetch = Mock(side_effect=AssertionError("unexpected fetch"))
        other = LocalComponentToolBinding(sys.executable, ("-I",))
        with self.assertRaises(ActionWireError):
            self.execute(self.worker(bindings=(other,)), blob_source=fetch)
        fetch.assert_not_called()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_corrupt_source_and_build_refuse_before_job_allocation(self):
        f = self.fixture
        for corrupt in (
            f.value.build_input.files[0].blob,
            f.value.build_result.artifact_archive,
        ):
            with self.subTest(reference=corrupt.identity):

                def fetch(reference, corrupt=corrupt):
                    return (
                        b"corrupt"
                        if reference == corrupt
                        else f.source_cas.get_bytes(reference)
                    )

                with (
                    patch(
                        "literate_ai.adapters.action_execute_worker.tempfile.mkdtemp",
                        side_effect=AssertionError("allocated before verification"),
                    ),
                    self.assertRaises(ActionWireError),
                ):
                    self.execute(blob_source=fetch)
                self.assertEqual(list(self.workspace.iterdir()), [])

    def test_profile_drift_during_transfer_refuses_before_allocation(self):
        worker = self.worker()

        def fetch(reference):
            worker.environment = MappingProxyType({"CHANGED": "yes"})
            return self.fixture.source_cas.get_bytes(reference)

        with self.assertRaises(ActionWireError) as error:
            self.execute(worker, blob_source=fetch)
        self.assertEqual(error.exception.code, "action_execute.profile_changed")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_failed_and_cancelled_child_cleanup_readonly_files(self):
        for cancel in (False, True):
            marker = self.fixture.fixture.root / ("cancelled" if cancel else "failed")
            code = (
                "from pathlib import Path; import time; "
                "p=Path('partial'); p.write_bytes(b'partial'); p.chmod(0o444); "
                f"Path({str(marker)!r}).touch(); "
                + ("time.sleep(60)" if cancel else "raise SystemExit(2)")
            )
            worker = self.worker(
                launcher=LocalComponentToolBinding(sys.executable, ("-c", code))
            )
            with self.subTest(cancel=cancel), self.assertRaises(ActionWireError):
                self.execute(
                    worker, cancelled=marker.exists if cancel else lambda: False
                )
            self.assertTrue(marker.exists())
            self.assertEqual(list(self.workspace.iterdir()), [])

    def receive(self, worker):
        output = io.BytesIO()
        wire = encode_action_request(self.request, self.fixture.deadline, self.records)
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
                {
                    "LITAI_ACTION_WORKER_IDENTITY": (
                        self.request.worker.worker_identity.uri
                    )
                },
            ),
        ):
            status = main(
                [
                    "--cas",
                    str(self.fixture.cas.root),
                    "--workspace",
                    str(self.workspace),
                ],
                execute_worker=worker,
            )
        self.assertEqual(status, 0)
        return decode_action_response(output.getvalue(), self.request)

    def test_unconfigured_receiver_refuses_execute(self):
        outcome, result = self.receive(None)
        self.assertEqual(outcome.failure_code, "action_execute.not_configured")
        self.assertIsNone(result)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_real_configured_child_executes_without_controller_source(
        self,
    ):
        self.real_child(via_receiver=False)

    def test_real_receiver_routes_execute_to_configured_child(self):
        self.real_child(via_receiver=True)

    def real_child(self, *, via_receiver):
        f = self.fixture
        shutil.rmtree(
            f.ports.source_trees.resolve(f.value.build_input.candidate.tree_identity)
        )
        code = """
import os,json,sys
from pathlib import Path
from literate_ai.execute_worker import main
from literate_ai.adapters.lifecycle import (
 LocalStandardLifecyclePorts,LocalComponentToolBinding,
)
from literate_ai.contracts import ComponentCommandPhase,ComponentCommandContract
from contextlib import contextmanager
@contextmanager
def factory(admitted,registry,recorder):
 contract=ComponentCommandContract.from_dict(json.loads(os.environ['EXECUTE_CONTRACT']))
 ports=LocalStandardLifecyclePorts(source_trees=registry,object_root=Path(os.environ['LITAI_EXECUTE_WORKSPACE'])/'objects',contracts=(contract,),tool_bindings=(LocalComponentToolBinding(sys.executable),),command_phases=(ComponentCommandPhase.EXECUTE,))
 ports.retain_evidence_with(recorder)
 yield ports
raise SystemExit(main(runtime_factory=factory))
"""
        runtime = discover_python_toolchain(pinned_command=(sys.executable,))
        launcher = LocalComponentToolBinding(
            sys.executable,
            ("-c", code),
            authority_identity=canonical_identity(
                {"runtime": runtime.identity, "code": code}
            ),
            _authority_guard=runtime.require_unchanged,
        )
        worker = self.worker(
            launcher=launcher,
            environment=dict(
                os.environ,
                PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                EXECUTE_CONTRACT=json.dumps(
                    f.value.build_input.inputs.contract.to_dict()
                ),
            ),
        )
        if via_receiver:
            # This receiver uses its private CAS; direct execution checks transfer.
            for reference in (
                *[item.blob for item in f.value.build_input.files],
                f.value.build_result.artifact_archive,
                *f.value.build_result.evidence_records,
            ):
                f.cas.put_bytes(
                    f.source_cas.get_bytes(reference), media_type=reference.media_type
                )
            outcome, content = self.receive(worker)
            self.assertIsNone(outcome.failure_code)
        else:
            content = self.execute(worker)
        result = ExecuteWorkerResult.admit(
            content,
            record_identity(content),
            input_record=f.content,
            input_identity=f.identity,
            deadline=f.deadline,
        )
        self.assertEqual(result.evidence.execution_authority.input_scope, f.value.scope)
        self.assertEqual(list(self.workspace.iterdir()), [])
        for reference in result.evidence_records:
            f.cas.verify(reference)
