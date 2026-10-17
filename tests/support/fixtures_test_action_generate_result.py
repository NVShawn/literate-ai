"""Generation results require exact source proof and current bounded input custody."""

import unittest
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.action_generate_result import (
    GenerateWorkerResult,
    capture_generate_result,
)
from literate_ai.contracts import (
    ContentIdentity,
)
from tests.support import (
    fixtures_test_cached_coding_cli_source_generation_runner as fixture_module,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class GenerateWorkerResultTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.CachedCodingCliSourceGenerationRunnerTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        _, execution = _fixture()
        self.value = GenerateWorkerInput.capture(
            execution, f.node, f.cas, self.deadline
        )
        self.input = self.value.to_bytes()
        self.identity = record_identity(self.input)
        self.output = f.runner(f.node)
        self.result = self.capture(self.output)

    def capture(self, output):
        return capture_generate_result(
            input_record=self.input,
            input_identity=self.identity,
            output=output,
            cas=self.fixture.cas,
            deadline=self.deadline,
        )

    def records(self, result=None):
        return tuple(
            (ContentIdentity.parse_uri(ref.identity), self.fixture.cas.get_bytes(ref))
            for ref in (result or self.result).evidence_records
        )

    def admit(self, result=None, input_record=None):
        raw = (result or self.result).to_bytes()
        source = self.input if input_record is None else input_record
        return GenerateWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=source,
            input_identity=record_identity(source),
            deadline=self.deadline,
        )
