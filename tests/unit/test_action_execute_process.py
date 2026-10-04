"""Actual EXECUTE children preserve authority, private controls and execution bounds."""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import (
    record_identity,
)
from literate_ai.adapters.action_execute_process import run_execute_worker_process
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_identity
from tests.support import fixtures_test_action_execute_execution as execution_fixture


class ActionExecuteProcessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = execution_fixture.ActionExecuteExecutionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.fixture.root

    def run_child(self, code, **changes):
        f = self.fixture
        arguments = dict(
            launcher=LocalComponentToolBinding(sys.executable, ("-c", code)),
            input_record=f.content,
            input_identity=f.identity,
            deadline=f.deadline,
            cwd=self.root,
            environment=dict(os.environ),
        )
        arguments.update(changes)
        return run_execute_worker_process(**arguments)

    def test_real_stdio_child_executes_from_cas_and_cleans_owned_workspace(self):
        f = self.fixture
        for reference in f.source_cas.iter_refs():
            f.cas.put_bytes(
                f.source_cas.get_bytes(reference), media_type=reference.media_type
            )
        shutil.rmtree(
            f.ports.source_trees.resolve(f.value.build_input.candidate.tree_identity)
        )
        workspace = self.root / "test-child"
        workspace.mkdir()
        code = """
import os,json,sys,shutil
from pathlib import Path
from contextlib import contextmanager
from literate_ai.execute_worker import main
from literate_ai.adapters.lifecycle import (
 LocalStandardLifecyclePorts,LocalComponentToolBinding,
)
from literate_ai.contracts import ComponentCommandPhase,ComponentCommandContract
@contextmanager
def factory(admitted,registry,recorder):
 print('private runtime starts')
 contract=ComponentCommandContract.from_dict(json.loads(os.environ['EXECUTE_CONTRACT']))
 ports=LocalStandardLifecyclePorts(source_trees=registry,object_root=Path(os.environ['LITAI_EXECUTE_WORKSPACE'])/'objects',contracts=(contract,),tool_bindings=(LocalComponentToolBinding(sys.executable),),command_phases=(ComponentCommandPhase.EXECUTE,))
 ports.retain_evidence_with(recorder)
 try:yield ports
 finally:shutil.rmtree(ports.object_root)
raise SystemExit(main(runtime_factory=factory))
"""
        env = dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
            EXECUTE_CONTRACT=json.dumps(f.value.build_input.inputs.contract.to_dict()),
        )
        runtime = discover_python_toolchain(pinned_command=(sys.executable,))
        launcher = LocalComponentToolBinding(
            sys.executable,
            ("-c", code),
            authority_identity=canonical_identity(
                {"runtime": runtime.identity, "code": code}
            ),
            _authority_guard=runtime.require_unchanged,
        )

        def checked_process(*args, **kwargs):
            completed = run_bounded_process(*args, **kwargs)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed

        with patch(
            "literate_ai.adapters.action_worker_process.run_bounded_process",
            side_effect=checked_process,
        ):
            content = self.run_child(
                code,
                environment=env,
                cas_root=f.cas.root,
                workspace_root=workspace,
                launcher=launcher,
            )
        result = ExecuteWorkerResult.admit(
            content,
            record_identity(content),
            input_record=f.content,
            input_identity=f.identity,
            deadline=f.deadline,
        )
        self.assertEqual(result.evidence.execution_authority.input_scope, f.value.scope)
        self.assertEqual(list(workspace.iterdir()), [])
        for reference in result.evidence_records:
            f.cas.verify(reference)
