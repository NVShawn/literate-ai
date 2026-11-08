"""Owned runtime state is separate from generated output and authentication."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.models import (
    CodingCliError,
    CodingCliSourceGenerator,
    CodingCliTaskRunner,
)
from literate_ai.adapters.models import coding_cli as module
from tests.support.fixtures_test_coding_cli_generation import (
    flavor,
    recipe,
    write_generated_test_suite,
)


class CodexRuntimeStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.auth = self.home / "auth"
        self.auth.mkdir()
        self.sentinel = self.auth / "unchanged"
        self.sentinel.write_text("operator-owned fixture")
        self.runtime = self.root / "temporary state"
        self.runtime.mkdir()
        self.environment = {
            "CODING_CLI": "codex",
            "PATH": "/tools",
            "HOME": str(self.home),
            "CODEX_HOME": str(self.auth),
            "OPENAI_API_KEY": "inert-fixture-key",
            "TMPDIR": str(self.runtime),
        }
        self.observed = []
        for patcher in (
            mock.patch.object(module.shutil, "which", return_value=sys.executable),
            mock.patch.object(
                module, "_require_codex_linux_workspace_write_prerequisite"
            ),
            mock.patch.dict(
                os.environ, {"LITAI_EVIDENCE_RUN": "", "LITAI_EVIDENCE_PARENT": ""}
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def observe_state(self, command, arguments):
        overrides = {}
        for index, argument in enumerate(command[:-1]):
            if argument == "-c":
                name, encoded = command[index + 1].split("=", 1)
                overrides[name] = Path(json.loads(encoded))
        self.assertEqual(set(overrides), {"sqlite_home", "log_dir"})
        workspace = Path(arguments["cwd"])
        for path in overrides.values():
            self.assertTrue(path.is_dir())
            self.assertFalse(path.is_relative_to(workspace))
            self.assertEqual(path.parent.parent, self.runtime)
            self.assertEqual(list(path.iterdir()), [])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            (path / "runtime-data").write_bytes(b"provider-owned fixture data")
        environment = arguments["environment"]
        for name in ("HOME", "CODEX_HOME", "OPENAI_API_KEY"):
            self.assertEqual(environment[name], self.environment[name])
        self.observed.append(overrides)
        return workspace

    def assert_retired(self):
        self.assertTrue(self.observed)
        self.assertTrue(
            all(not path.exists() for row in self.observed for path in row.values())
        )
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.assertEqual(self.sentinel.read_text(), "operator-owned fixture")

    def test_generation_excludes_and_retires_runtime_data(self):
        value = recipe(flavor("cpp"))

        def execute(command, **arguments):
            workspace = self.observe_state(command, arguments)
            self.assertEqual(command[command.index("--model") + 1], "base-codex-model")
            source = workspace / "source/main.cpp"
            source.parent.mkdir()
            source.write_text("int main() { return 0; }\n")
            write_generated_test_suite(workspace, value)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(module, "_run_bounded", side_effect=execute):
            generation = CodingCliSourceGenerator(
                environment=self.environment
            ).generate(value, output_root=self.root / "generated")
        self.assertFalse(any("runtime-data" in name for name in generation.files))
        self.assertIn("source/main.cpp", generation.files)
        self.assert_retired()

    def test_json_task_keeps_only_its_response_and_retires_runtime_data(self):
        def execute(command, **arguments):
            workspace = self.observe_state(command, arguments)
            self.assertEqual(command[command.index("--model") + 1], "fixture-model")
            (workspace / ".literate-ai-task-response.json").write_text('{"value":42}')
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(module, "_run_bounded", side_effect=execute):
            result = CodingCliTaskRunner(environment=self.environment).run_json_task(
                "Return the fixture value.", model="fixture-model"
            )
        self.assertEqual(result.response, {"value": 42})
        self.assert_retired()

    def test_both_execution_paths_retire_state_on_bounded_failure(self):
        def execute(command, **arguments):
            self.observe_state(command, arguments)
            raise CodingCliError("coding_cli.timeout", "controlled transport timeout")

        with mock.patch.object(module, "_run_bounded", side_effect=execute):
            for kind in ("source", "json"):
                with self.subTest(kind=kind), self.assertRaises(CodingCliError):
                    if kind == "source":
                        CodingCliSourceGenerator(environment=self.environment).generate(
                            recipe(flavor("cpp")), output_root=self.root / "failed"
                        )
                    else:
                        CodingCliTaskRunner(environment=self.environment).run_json_task(
                            "Return a fixture value."
                        )
                self.assert_retired()

    def test_repeated_calls_get_distinct_owned_state(self):
        def execute(command, **arguments):
            workspace = self.observe_state(command, arguments)
            (workspace / ".literate-ai-task-response.json").write_text("{}")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(module, "_run_bounded", side_effect=execute):
            runner = CodingCliTaskRunner(environment=self.environment)
            runner.run_json_task("Return an empty fixture object.")
            self.assert_retired()
            runner.run_json_task("Return another empty fixture object.")
        self.assertNotEqual(self.observed[0], self.observed[1])
        self.assert_retired()

    def test_other_provider_receives_no_codex_state_options(self):
        def execute(command, **arguments):
            self.assertNotIn("-c", command)
            self.assertEqual(arguments["environment"]["HOME"], str(self.home))
            self.assertEqual(list(self.runtime.iterdir()), [])
            (Path(arguments["cwd"]) / ".literate-ai-task-response.json").write_text(
                "{}"
            )
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(module, "_run_bounded", side_effect=execute):
            result = CodingCliTaskRunner(
                environment={**self.environment, "CODING_CLI": "claude"}
            ).run_json_task("Return an empty fixture object.")
        self.assertEqual(result.response, {})
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_state_inside_generated_output_refuses_and_removes_owned_directory(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        with self.assertRaisesRegex(CodingCliError, "outside the generated workspace"):
            with module._coding_cli_runtime_state(
                SimpleNamespace(name="codex"),
                workspace=workspace,
                environment={"TMPDIR": str(workspace)},
            ):
                self.fail("state inside generated output was admitted")
        self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
