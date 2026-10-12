"""Generation wire custody reopens exact bounded prompt authority without host paths."""

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.retained_source import RetainedSourceInput
from literate_ai.contracts import canonical_identity
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_source_generation_scheduling import _PreparedFixture


class GenerateWorkerInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _PreparedFixture()
        self.addCleanup(self.fixture.close)
        self.prepared = self.fixture.nodes[0]
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        self.cas = FileSystemCAS(Path(self.fixture.temporary.name) / "wire-cas")
        self.value = GenerateWorkerInput.capture(
            self.fixture.execution, self.prepared, self.cas, self.deadline
        )
        self.content = self.value.to_bytes()

    def admit(self, content=None, deadline=None):
        raw = self.content if content is None else content
        return GenerateWorkerInput.admit(
            raw, record_identity(raw), deadline or self.deadline
        )

    def retained(self, lock=None):
        root = Path(self.fixture.temporary.name) / "retained"
        root.mkdir(exist_ok=True)
        (root / "main.py").write_bytes(b"pass\n")
        return RetainedSourceInput.capture(
            root.resolve(),
            component_lock_identity=lock
            or self.fixture.execution.component_lock_identity,
            project_authority_identity=canonical_identity("project"),
            target="host",
        )
