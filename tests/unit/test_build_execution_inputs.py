"""Remote BUILD input custody retains current grants without host execution."""

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.lifecycle import (
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
)
from literate_ai.contracts import ComponentCommandPhase
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
)


class BuildExecutionInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ports, self.old_plan, self.candidate, self.intent = _python_copy_lifecycle(
            self.root
        )
        self.authorization = self.ports.authorize(self.intent, _identity("new-index"))
        self.inputs = self.ports.plan_finalization_inputs(
            self.intent, self.authorization
        )
        self.plan = self.inputs.finalize()

    def test_unregistered_and_superseded_plans_cannot_recover_grants(self):
        with self.assertRaisesRegex(LocalStandardLifecycleError, "registered"):
            self.ports.build_execution_inputs(self.plan)
        self.ports.accept_finalized_plan(self.intent, self.authorization, self.plan)
        self.assertEqual(self.ports.build_execution_inputs(self.plan), self.inputs)
        with self.assertRaisesRegex(LocalStandardLifecycleError, "registered"):
            self.ports.build_execution_inputs(self.old_plan)

    def test_data_custody_does_not_enable_build_on_a_test_only_receiver(self):
        receiver = LocalStandardLifecyclePorts(
            source_trees=self.ports.source_trees,
            object_root=self.root / "test-worker",
            contracts=tuple(self.ports.contracts.values()),
            tool_bindings=tuple(self.ports.tool_bindings.values()),
            command_phases=(ComponentCommandPhase.TEST,),
        )
        _, execution = _fixture()
        intent = receiver.create(
            execution, execution.generation_plans[0], self.candidate, (), ()
        )
        receiver.accept_finalized_plan(intent, self.authorization, self.plan)
        self.assertEqual(receiver.build_execution_inputs(self.plan), self.inputs)
        with self.assertRaisesRegex(LocalStandardLifecycleError, "scope"):
            receiver.build(self.plan, ())
        self.assertEqual(list(receiver.object_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
