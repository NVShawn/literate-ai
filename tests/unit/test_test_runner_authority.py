from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from literate_ai.projects import load_project
from literate_ai.test_runner_authority import (
    SAMPLE_TEST_RUNNER_SOURCE_PATHS,
    sample_test_runner_source_closure_identity,
)

REPOSITORY = Path(__file__).resolve().parents[2]
RUNNER_MODULE = REPOSITORY / "tests/conformance/support/sample_runner.py"


class SampleTestRunnerAuthorityTests(unittest.TestCase):
    def test_repository_owned_support_imports_are_in_the_reviewed_closure(self):
        syntax = ast.parse(RUNNER_MODULE.read_text(encoding="utf-8"))
        imported = {
            module.replace(".", "/") + ".py"
            for node in ast.walk(syntax)
            if isinstance(node, ast.ImportFrom)
            and isinstance(node.module, str)
            and node.module.startswith("tests.conformance.support.")
            for module in (node.module,)
        }

        self.assertEqual(
            imported,
            {
                "tests/conformance/support/durable_split_service.py",
                "tests/conformance/support/runtime_oracles.py",
                "tests/conformance/support/standard_service_stack.py",
            },
        )
        self.assertTrue(imported <= set(SAMPLE_TEST_RUNNER_SOURCE_PATHS))
        project = load_project(REPOSITORY)
        driver = project.definition.lifecycle_driver
        self.assertIsNotNone(driver)
        assert driver is not None
        self.assertTrue(imported <= set(driver.implementation_paths))

    def test_durable_portfolio_bytes_change_the_reviewed_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in SAMPLE_TEST_RUNNER_SOURCE_PATHS:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((REPOSITORY / relative).read_bytes())
            initial = sample_test_runner_source_closure_identity(root)
            durable = root / "tests/conformance/support/durable_split_service.py"
            durable.write_bytes(durable.read_bytes() + b"\n")
            changed = sample_test_runner_source_closure_identity(root)

        self.assertNotEqual(initial, changed)


if __name__ == "__main__":
    unittest.main()
