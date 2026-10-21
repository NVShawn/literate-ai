"""Native inspectors enforce process budgets before consuming unbounded output."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import builders
from literate_ai.adapters.dependencies import observation
from literate_ai.adapters.dependencies.types import DependencyObservationError


class DependencyInspectorProcessTests(unittest.TestCase):
    def test_inspector_does_not_inherit_loader_or_python_injection(self):
        program = """
import json, os
print(json.dumps([os.getenv("LD_PRELOAD"), os.getenv("PYTHONPATH")]))
"""
        with patch.dict(os.environ, {"LD_PRELOAD": "fixture", "PYTHONPATH": "fixture"}):
            output = observation._run_bounded_tool(
                Path(sys.executable).resolve(),
                ("-c", program),
                code="dependencies.fixture",
            )
        self.assertEqual(json.loads(output), [None, None])

    def test_inspector_output_overflow_is_a_typed_failure(self):
        with patch.object(observation, "_MAX_TOOL_OUTPUT", 128):
            with self.assertRaises(DependencyObservationError) as raised:
                observation._run_bounded_tool(
                    Path(sys.executable).resolve(),
                    ("-c", 'print("x" * 4096)'),
                    code="dependencies.fixture-output",
                )
        self.assertEqual(raised.exception.code, "dependencies.fixture-output")

    def test_inspector_deadline_is_enforced_by_the_owned_process_runner(self):
        original = builders.run_bounded_process

        def short_deadline(*args, **kwargs):
            self.assertEqual(kwargs["timeout_seconds"], 30)
            self.assertTrue(kwargs["terminate_descendants"])
            kwargs["timeout_seconds"] = 0.1
            return original(*args, **kwargs)

        with patch.object(builders, "run_bounded_process", side_effect=short_deadline):
            with self.assertRaises(DependencyObservationError) as raised:
                observation._run_bounded_tool(
                    Path(sys.executable).resolve(),
                    ("-c", "import time; time.sleep(10)"),
                    code="dependencies.fixture-timeout",
                )
        self.assertEqual(raised.exception.code, "dependencies.fixture-timeout")


if __name__ == "__main__":
    unittest.main()
