"""Command EXECUTE transport verifies returned proof using shared admitted capacity."""

import os
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_executor import CommandComponentExecutor
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from tests.support import fixtures_test_action_execute_result as result_fixture


class CommandExecutorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = result_fixture.ActionExecuteResultTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.value = f.fixture.value
        self.ports = f.ports
        self.scope = self.value.scope
        self.providers = self.value.provider_artifacts
        self.plan = self.value.build_input.plan
        self.exports = self.value.build_result.evidence.exports
        root = f.fixture.fixture.root
        self.marker = root / "test-dispatched"
        input_path, result_path = root / "test-input.json", root / "test-result.json"
        input_path.write_bytes(f.fixture.content)
        result_path.write_bytes(f.content)
        # The child returns real previously produced EXECUTE proof for this exact input.
        # Configured receiver tests separately exercise the actual EXECUTE child.
        code = f"""
import sys
from pathlib import Path
from literate_ai.adapters.action_dispatch_wire import (
 decode_action_request,encode_action_response,MAX_ACTION_WIRE_BYTES,
)
from literate_ai.adapters.action_execute import admit_execute_action
request,deadline,records=decode_action_request(sys.stdin.buffer.read(MAX_ACTION_WIRE_BYTES+1))
admit_execute_action(request,deadline,records,expected_worker_identity=request.worker.worker_identity)
expected = Path({str(input_path)!r}).read_bytes()
assert records[request.predecessor_result_identities[0]] == expected
Path({str(self.marker)!r}).touch()
sys.stdout.buffer.write(encode_action_response(request,result_record=Path({str(result_path)!r}).read_bytes()))
"""
        self.worker = ExecutionWorker(
            "tester",
            ExecutionWorkerKind.COMMAND,
            command=(sys.executable, "-I", "-c", code),
        )
        self.catalog = ExecutionWorkerCatalog((self.worker,))
        self.admitted = LifecycleActionWorker(
            self.worker.worker_id,
            self.worker.identity,
            self.catalog.identity,
            canonical_identity("hardware"),
        )
        self.healthy = True
        self.tools_available = True

        def revalidate(worker):
            self.assertEqual(worker, self.admitted)
            if not self.healthy:
                raise ActionWireError("fixture.changed", "worker changed")

        self.indexer = CommandGenerationIndexer(
            self.value.build_input.execution_plan,
            self.ports.source_trees,
            lambda source: self.ports.source_trees.evidence(source).candidate,
            f.cas,
            self.catalog,
            (self.admitted,),
            f.fixture.deadline,
            cwd=root,
            revalidate_worker=revalidate,
            environment=dict(os.environ),
        )
        self.admission = SimpleNamespace(
            catalog=self.catalog,
            workers=(self.admitted,),
            identity=canonical_identity("admission"),
            supports_phase=lambda worker, phase: (
                worker == self.admitted and phase is LifecycleActionKind.EXECUTE
            ),
            supports_execute=lambda worker, tools: (
                self.tools_available and worker == self.admitted
            ),
        )
        self.executor = CommandComponentExecutor(
            self.indexer,
            self.admission,
            self.ports,
            handoff_for=lambda *args: self.value,
            result_source=self.fetch,
        )
        self.executor.retain_execution_provider_evidence(
            self.plan, self.scope, self.value.accepted_providers
        )

    def fetch(self, worker, reference):
        self.assertEqual(worker, self.worker)
        return self.fixture.fixture.cas.get_bytes(reference)

    def test_receipt_delivery_is_idempotent_and_refuses_foreign_scope(self):
        self.executor.retain_execution_provider_evidence(self.plan, self.scope, ())
        self.assertEqual(len(self.executor._receipts), 1)
        for changes in (
            {"execution_plan_identity": canonical_identity("foreign-execution")},
            {"build_plan_identity": canonical_identity("foreign-build")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ActionWireError):
                self.executor.retain_execution_provider_evidence(
                    self.plan, replace(self.scope, **changes), ()
                )
        with self.assertRaises(ActionWireError):
            self.executor.retain_execution_provider_evidence(
                self.plan, self.scope, (object(),)
            )
        self.assertEqual(len(self.executor._receipts), 1)
