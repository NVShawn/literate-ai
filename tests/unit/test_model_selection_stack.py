"""Per-agent model stack never empties; pop of the last frame warns with location."""

from __future__ import annotations

import contextvars
import json
import tempfile
import threading
import unittest
import warnings
from pathlib import Path

from literate_ai.adapters.live_test_selection import (
    LiveTestSelection,
    log_live_session_models,
)
from literate_ai.adapters.model_selection_stack import (
    ModelStackError,
    ModelStackOperation,
    ModelStackPopRefusedWarning,
    apply_spec_stack_operations,
    begin_session,
    current,
    depth,
    parse_model_stack_document,
    parse_model_stack_operations,
    pop,
    push,
    query_spec_stack_operation,
    reset_session,
    snapshot,
)
from literate_ai.diagnostics import operation_log


class ModelSelectionStackTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_session()

    def tearDown(self) -> None:
        reset_session()

    def test_session_starts_at_depth_one(self) -> None:
        begin_session(coding_cli="opencode", model="openai/gpt-5")
        self.assertEqual(depth(), 1)
        self.assertEqual(current().model, "openai/gpt-5")
        self.assertEqual(current().coding_cli, "opencode")

    def test_push_and_pop_restore_the_session_model(self) -> None:
        begin_session(coding_cli="opencode", model="openai/gpt-5")
        push(model="openai/o3", reason="component", location="component.md")
        self.assertEqual(depth(), 2)
        self.assertEqual(current().model, "openai/o3")
        pop(location="component.md")
        self.assertEqual(depth(), 1)
        self.assertEqual(current().model, "openai/gpt-5")

    def test_pop_of_the_only_frame_warns_and_keeps_depth_one(self) -> None:
        begin_session(coding_cli="opencode", model="openai/gpt-5")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            pop(location="component.md:42 section=Core")
        self.assertEqual(depth(), 1)
        self.assertEqual(current().model, "openai/gpt-5")
        refused = [
            item
            for item in caught
            if issubclass(item.category, ModelStackPopRefusedWarning)
        ]
        self.assertEqual(len(refused), 1)
        message = str(refused[0].message)
        self.assertIn("test_model_selection_stack.py", message)
        self.assertIn("test_pop_of_the_only_frame_warns_and_keeps_depth_one", message)
        self.assertIn("component.md:42", message)
        self.assertIn("section=Core", message)

    def test_pop_refused_log_records_caller_file_line_and_spec_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ops.ndjson"
            with operation_log(path):
                begin_session(coding_cli="opencode", model="openai/gpt-5")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ModelStackPopRefusedWarning)
                    pop(location="flavor.md:9 section=algorithm")
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            ]
        refused = [
            record
            for record in records
            if record.get("event") == "model.stack.pop_refused"
        ]
        self.assertEqual(len(refused), 1)
        record = refused[0]
        self.assertEqual(record["model_stack_depth"], 1)
        self.assertEqual(record["location"], "flavor.md:9 section=algorithm")
        self.assertEqual(
            record["caller_function"],
            "test_pop_refused_log_records_caller_file_line_and_spec_location",
        )
        self.assertIn("test_model_selection_stack.py", record["caller"])
        self.assertIn("test_model_selection_stack.py", record["caller_file"])
        self.assertIsInstance(record["caller_line"], int)
        self.assertGreater(record["caller_line"], 0)

    def test_querying_without_a_session_fails_closed_instead_of_depth_zero(
        self,
    ) -> None:
        with self.assertRaises(ModelStackError) as raised:
            depth()
        self.assertEqual(raised.exception.code, "model_stack.empty")

    def test_threads_isolate_stacks(self) -> None:
        results: dict[str, tuple[int, str]] = {}
        errors: list[Exception] = []

        def worker(name: str, model: str) -> None:
            try:
                begin_session(coding_cli="opencode", model=model)
                push(model=f"{model}-inner", reason="section", location=name)
                results[name] = (depth(), current().model)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        first = threading.Thread(target=worker, args=("a", "m1"))
        second = threading.Thread(target=worker, args=("b", "m2"))
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(errors, [])
        self.assertEqual(results["a"], (2, "m1-inner"))
        self.assertEqual(results["b"], (2, "m2-inner"))
        with self.assertRaises(ModelStackError):
            snapshot()

    def test_copied_context_isolates_agent_stacks(self) -> None:
        begin_session(coding_cli="opencode", model="parent")
        copied = contextvars.copy_context()

        def agent() -> None:
            begin_session(coding_cli="opencode", model="agent")
            push(model="agent-section", reason="section", location="agent.md")
            self.assertEqual(depth(), 2)
            self.assertEqual(current().model, "agent-section")

        copied.run(agent)
        self.assertEqual(depth(), 1)
        self.assertEqual(current().model, "parent")

    def test_specification_language_push_pop_and_depth_query(self) -> None:
        text = (
            '<!-- literate-ai:model-stack op="push" model="openai/o3" '
            'section="algorithm" -->\n'
            "body\n"
            '<!-- literate-ai:model-stack op="pop" section="algorithm" -->\n'
        )
        operations = parse_model_stack_operations(text, source="component.md")
        self.assertEqual(len(operations), 2)
        self.assertEqual(operations[0].op, "push")
        self.assertEqual(operations[0].model, "openai/o3")
        self.assertEqual(operations[0].section, "algorithm")
        self.assertEqual(operations[0].source, "component.md")
        self.assertEqual(operations[0].line, 1)
        begin_session(coding_cli="opencode", model="openai/gpt-5")
        push(model="openai/o3", reason="section", location=operations[0].location())
        self.assertEqual(query_spec_stack_operation(ModelStackOperation("depth")), 2)
        self.assertEqual(
            query_spec_stack_operation(ModelStackOperation("current")),
            "openai/o3",
        )
        apply_spec_stack_operations(operations[1:], default_cli="opencode")
        self.assertEqual(depth(), 1)
        self.assertEqual(current().model, "openai/gpt-5")

    def test_specification_language_pop_of_last_frame_uses_directive_location(
        self,
    ) -> None:
        text = '<!-- literate-ai:model-stack op="pop" section="Core" -->'
        operations = parse_model_stack_operations(text, source="component.md")
        begin_session(coding_cli="opencode", model="openai/gpt-5")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            apply_spec_stack_operations(operations, default_cli="opencode")
        self.assertEqual(depth(), 1)
        message = str(caught[0].message)
        self.assertIn("component.md:1", message)
        self.assertIn("section=Core", message)

    def test_coding_model_selection_v2_stack_document(self) -> None:
        operations = parse_model_stack_document(
            {
                "schema": "literate-ai/coding-model-selection@2",
                "models": {"opencode": "openai/gpt-5"},
                "stack": [
                    {"op": "push", "model": "openai/o3", "section": "algorithm"},
                    {"op": "pop", "section": "algorithm"},
                ],
            },
            source="model-selections/core.json",
        )
        self.assertEqual(len(operations), 2)
        self.assertEqual(operations[0].model, "openai/o3")
        self.assertEqual(operations[0].source, "model-selections/core.json")

    def test_live_session_logs_user_default_and_cli_override(self) -> None:
        selection = LiveTestSelection(
            "codex",
            "from-flag",
            "cli-flag",
            "cli-flag",
            user_coding_cli="opencode",
            user_model="from-file",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ops.ndjson"
            with operation_log(path):
                log_live_session_models(selection)
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            ]
        events = [record["event"] for record in records]
        self.assertIn("model.session.user_default", events)
        self.assertIn("model.session.cli_override", events)
        override = next(
            record
            for record in records
            if record["event"] == "model.session.cli_override"
        )
        self.assertEqual(override["coding_cli"], "codex")
        self.assertEqual(override["model"], "from-flag")
        self.assertEqual(override["user_coding_cli"], "opencode")
        self.assertEqual(override["user_model"], "from-file")


if __name__ == "__main__":
    unittest.main()
