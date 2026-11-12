"""Exact callback identity removes duplicate calls, never live input checks."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalStandardLifecycleError,
)
from literate_ai.adapters.retained_cargo_execution_v2 import (
    _require_distinct_extra_guard,
)
from literate_ai.projects import PinnedInputClosure


class RetainedCargoGuardDedupV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.python = discover_python_toolchain(
            {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
            pinned_command=(sys.executable,),
        )

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.input = self.root / "authority-input"
        self.input.write_bytes(b"original guarded input")
        self.closure = PinnedInputClosure(
            maximum_files=1, maximum_file_bytes=64, maximum_total_bytes=64
        )
        self.closure.pin(self.input, boundary=self.root, label="owned guard input")
        self.calls = []

    def tool(self, label, *, binding_class=LocalComponentToolBinding):
        def current():
            self.calls.append(label)
            self.python.require_unchanged()
            self.closure.require_unchanged()

        return binding_class.from_observed_toolchain(self.python, guard=current)

    def boundary(self, tool, extra):
        _require_distinct_extra_guard(tool, extra)
        tool.require_unchanged()

    def test_identical_guard_runs_once_at_each_boundary(self):
        tool = self.tool("tool")
        self.boundary(tool, tool._authority_guard)
        self.boundary(tool, tool._authority_guard)
        self.assertEqual(self.calls, ["tool", "tool"])

    def test_distinct_guard_is_not_discarded(self):
        tool = self.tool("tool")

        def extra():
            self.calls.append("extra")
            self.closure.require_unchanged()

        self.boundary(tool, extra)
        self.boundary(tool, extra)
        self.assertEqual(self.calls, ["extra", "tool", "extra", "tool"])

    def test_changed_current_tool_callback_is_not_a_cached_equivalence(self):
        original = self.tool("original")
        self.boundary(original, original._authority_guard)
        current = self.tool("current")
        self.assertEqual(original.toolchain_identity, current.toolchain_identity)
        self.calls.clear()
        self.boundary(current, original._authority_guard)
        self.assertEqual(self.calls, ["original", "current"])

    def test_same_guard_still_refuses_real_input_mutation_at_closing_boundary(self):
        tool = self.tool("tool")
        self.boundary(tool, tool._authority_guard)
        self.input.write_bytes(b"changed guarded input")
        with self.assertRaisesRegex(LocalStandardLifecycleError, "toolchain changed"):
            self.boundary(tool, tool._authority_guard)
        self.assertEqual(self.calls, ["tool", "tool"])
        self.assertEqual(self.input.read_bytes(), b"changed guarded input")

    def test_subclass_uses_conservative_distinct_guard_path(self):
        class SpecializedBinding(LocalComponentToolBinding):
            pass

        tool = self.tool("tool", binding_class=SpecializedBinding)
        self.boundary(tool, tool._authority_guard)
        self.assertEqual(self.calls, ["tool", "tool"])
