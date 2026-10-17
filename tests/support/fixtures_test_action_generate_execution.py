"""Worker GENERATE runs admitted source production and cleans only owned staging."""

import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_generate_execution import (
    GenerationWorkerRuntime,
    execute_worker_generation,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationRunner
from literate_ai.storage import FileSystemCAS
from tests.support import (
    fixtures_test_cached_coding_cli_source_generation_runner as fixture_module,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class GenerateExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.CachedCodingCliSourceGenerationRunnerTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        _, execution = _fixture()
        self.value = GenerateWorkerInput.capture(
            execution, f.node, f.cas, self.deadline
        )
        self.raw = self.value.to_bytes()
        self.identity = record_identity(self.raw)
        self.cas = FileSystemCAS(f.root / "worker-cas")
        self.jobs = f.root / "jobs"
        self.jobs.mkdir()
        self.opened = []
        self.healthy = True

    def guard(self):
        if not self.healthy:
            raise RuntimeError("private authority changed")

    @contextmanager
    def runtime(self, binding):
        f = self.fixture
        self.opened.append(binding)
        runner = CachedCodingCliSourceGenerationRunner(
            f.generator,
            cas=self.cas,
            invocation_provider=lambda _: f.invocation,
            workspace_binding=binding,
        )
        yield GenerationWorkerRuntime(f.node.definition, f.node.recipe, runner)

    def execute(self, **changes):
        arguments = dict(
            input_record=self.raw,
            input_identity=self.identity,
            deadline=self.deadline,
            cas=self.cas,
            workspace_root=self.jobs,
            runtime_factory=self.runtime,
            admission_guard=self.guard,
            blob_source=self.fixture.cas.get_bytes,
        )
        arguments.update(changes)
        return execute_worker_generation(**arguments)
