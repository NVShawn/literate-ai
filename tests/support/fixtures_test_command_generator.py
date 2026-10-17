"""Real GENERATE command transport, shared capacity and source proof import."""

import os
import sys
import unittest
from types import SimpleNamespace

import tests.support.fixtures_test_action_generate_source as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_generator import CommandSourceGenerator
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


class CommandGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.GenerateSourceTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        value = f.fixture.value
        root = f.fixture.fixture.root
        self.marker = root / "dispatched"
        result_path = root / "result.json"
        result_path.write_bytes(f.fixture.result.to_bytes())
        code = f"""
import sys
from pathlib import Path
from literate_ai.adapters.action_dispatch_wire import (
 decode_action_request,encode_action_response,MAX_ACTION_WIRE_BYTES,
)
from literate_ai.adapters.action_generate import admit_generate_action
request,deadline,records=decode_action_request(sys.stdin.buffer.read(MAX_ACTION_WIRE_BYTES+1))
admit_generate_action(request,deadline,records,expected_worker_identity=request.worker.worker_identity)
assert not request.action.predecessor_ids
assert not request.predecessor_result_identities
Path({str(self.marker)!r}).touch()
sys.stdout.buffer.write(encode_action_response(request,result_record=Path({str(result_path)!r}).read_bytes()))
"""
        self.worker = ExecutionWorker(
            "generator",
            ExecutionWorkerKind.COMMAND,
            command=(sys.executable, "-I", "-c", code),
        )
        self.catalog = ExecutionWorkerCatalog((self.worker,))
        self.admitted = LifecycleActionWorker(
            self.worker.worker_id,
            self.worker.identity,
            self.catalog.identity,
            canonical_identity("observed"),
        )
        self.healthy, self.available = (True, True)

        def revalidate(worker):
            self.assertEqual(worker, self.admitted)
            if not self.healthy:
                raise ActionWireError("fixture.changed", "worker changed")

        self.indexer = CommandGenerationIndexer(
            value.execution_plan,
            f.registry,
            lambda source: f.registry.evidence(source).candidate,
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
                self.available
                and worker == self.admitted
                and (phase is LifecycleActionKind.GENERATE)
            ),
        )
        self.generator = CommandSourceGenerator(
            self.indexer,
            self.admission,
            cache_key_provider=f.fixture.fixture.runner.planned_cache_key,
            result_source=self.fetch,
        )

    def fetch(self, worker, reference):
        self.assertEqual(worker, self.worker)
        return self.fixture.fixture.fixture.cas.get_bytes(reference)

    def reserve(self):
        return self.generator.try_reserve_generate(self.fixture.prepared)

    def assert_released(self):
        reservation = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reservation)
        reservation.release()
