"""Locally completed stages feed an isolated ACCEPT child without local acceptance."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.support.fixtures_test_command_builder as fixture_module
from literate_ai.adapters.accept_handoff import CompletedStagesAcceptHandoff
from literate_ai.adapters.action_accept_result import import_accept_result
from literate_ai.adapters.action_accept_worker import ConfiguredAcceptWorker
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.execute_handoff import CompletedBuildExecuteHandoff
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.local_test_handoff import LocalBuildTestHandoff
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import canonical_identity
from literate_ai.storage import FileSystemCAS
from tests.support.command_worker_fixture import _ACCEPT_CHILD
from tests.support.fixtures_test_action_accept_action import make_accept_request


class LocalAcceptHandoffTests(unittest.TestCase):
    def test_actual_local_stages_are_accepted_by_isolated_tool_free_child(self):
        fixture = fixture_module.CommandBuilderTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        ports, indexer, plan = fixture.ports, fixture.indexer, fixture.plan
        builder = LocalBuildTestHandoff(ports, indexer, ports)
        output = builder.build(plan, ())
        tests = ports.test(plan, output.exports)
        scope = plan_standard_execution_receipts(
            indexer.execution_plan, plan, output.exports, ()
        )
        execution = ports.execute_scoped(plan, output.exports, scope, ())
        handoff = CompletedStagesAcceptHandoff(
            indexer,
            ports,
            execution_input_for=CompletedBuildExecuteHandoff(builder, indexer, ports),
        )
        handoff.retain_execution_provider_evidence(plan, scope, ())
        runtime = discover_python_toolchain(pinned_command=(sys.executable,))
        worker = ConfiguredAcceptWorker(
            LocalComponentToolBinding(
                sys.executable,
                ("-c", _ACCEPT_CHILD),
                authority_identity=canonical_identity(
                    {"runtime": runtime.identity, "code": _ACCEPT_CHILD}
                ),
                _authority_guard=runtime.require_unchanged,
            ),
            environment=dict(
                os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")
            ),
        )
        self.assertEqual(worker.tools.identities, ())
        workspace = fixture.root / "accept-jobs"
        workspace.mkdir()
        cas = FileSystemCAS(fixture.root / "accept-worker-cas")
        with (
            patch.object(ports, "accept", side_effect=AssertionError("local ACCEPT")),
            patch.object(
                ports, "_run_locked", side_effect=AssertionError("local command")
            ),
        ):
            value = handoff(plan, tests.identity, execution.identity)
            request, records = make_accept_request(value, indexer.deadline)
            result = worker.execute(
                request=request,
                deadline=indexer.deadline,
                records=records,
                expected_worker_identity=request.worker.worker_identity,
                cas=cas,
                workspace_root=workspace,
                blob_source=indexer.cas.get_bytes,
            )
            content = value.to_bytes()
            accepted = import_accept_result(
                content=result,
                result_identity=record_identity(result),
                input_record=content,
                input_identity=record_identity(content),
                deadline=indexer.deadline,
                ports=ports,
                cas=indexer.cas,
                admission_guard=indexer.deadline.remaining,
                blob_source=cas.get_bytes,
            )
        self.assertEqual(accepted.build.identity, output.build_identity)
        self.assertEqual(accepted.generated_tests, tests)
        self.assertEqual(accepted.execution, execution)
        self.assertFalse(fixture.marker.exists(), "remote BUILD was dispatched")
        self.assertEqual(list(workspace.iterdir()), [])
