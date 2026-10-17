"""Controller source publication verifies full proof and exact workspace custody."""

import shutil
import unittest
from pathlib import Path

import tests.support.fixtures_test_action_generate_result as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_generate_source import receive_generated_source
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.storage import FileSystemCAS


class GenerateSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.GenerateWorkerResultTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.prepared = f.fixture.node
        self.root = Path(self.prepared.workspace.locator)
        shutil.rmtree(self.root)
        self.root.mkdir()
        self.cas = FileSystemCAS(f.fixture.root / "controller")
        self.registry = LocalSourceTreeRegistry()
        self.healthy = True

    def guard(self):
        if not self.healthy:
            raise ActionWireError("fixture.revoked", "authority revoked")

    def receive(self, **changes):
        f = self.fixture
        content = f.result.to_bytes()
        return receive_generated_source(
            **dict(
                content=content,
                result_identity=record_identity(content),
                input_record=f.input,
                input_identity=f.identity,
                prepared=self.prepared,
                deadline=f.deadline,
                cas=self.cas,
                registry=self.registry,
                admission_guard=self.guard,
                blob_source=f.fixture.cas.get_bytes,
            )
            | changes
        )
