"""Configured EXECUTE workers reject stale inputs before allocating owned jobs."""

import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.action_worker import main
from literate_ai.adapters.action_dispatch_wire import (
    decode_action_response,
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_identity
from tests.support import fixtures_test_action_execute_execution as execution_fixture
from tests.support.fixtures_test_action_execute_action import make_execute_request


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
