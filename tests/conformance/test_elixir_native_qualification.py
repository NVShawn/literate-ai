"""Opt-in native Elixir qualification of locked Standard commands.

These inert fixtures qualify the host adapters, never coding-provider generation.
Opting in requires the real runtime; build-system qualification additionally
requires Make and Bazel. Missing or incompatible tools fail instead of skipping.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.builders import discover_elixir_toolchain
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts import ComponentCommandPhase
from tests.unit.test_standard_command_projection import _locked_snapshot, _observation

NATIVE = os.environ.get("LITERATE_AI_ELIXIR_QUALIFICATION") == "1"
BUILD_SYSTEMS = os.environ.get("LITERATE_AI_ELIXIR_BUILD_SYSTEMS") == "1"
HOST = {"darwin": "macos", "win32": "windows"}.get(__import__("sys").platform, "linux")

_HELPER = "defmodule Fixture do\n  def sum(a, b), do: a + b\nend\n"
_TESTS = """unless Fixture.sum(20, 22) == 42, do: raise "addition failed"
IO.puts(JSON.encode!(%{schema: "literate-ai/generated-test-results@1",
  cases: [%{case_id: "fixture-example", outcome: "passed"}]}))
"""
_MAIN = """Code.require_file("helper.ex", __DIR__)
case System.argv() do
  ["--litai-test"] -> Code.require_file("tests/litai_test.exs", __DIR__)
  ["--litai-smoke"] -> IO.puts(JSON.encode!(Fixture.sum(20, 22)))
  [input] ->
    [a, b] = JSON.decode!(input)
    IO.puts(JSON.encode!(Fixture.sum(a, b)))
end
"""
_SINGLE = (
    _HELPER
    + """case System.argv() do
  ["--litai-test"] ->
    unless Fixture.sum(20, 22) == 42, do: raise "addition failed"
    IO.puts(JSON.encode!(%{schema: "literate-ai/generated-test-results@1",
      cases: [%{case_id: "fixture-example", outcome: "passed"}]}))
  ["--litai-smoke"] -> IO.puts(JSON.encode!(Fixture.sum(20, 22)))
  [input] ->
    [a, b] = JSON.decode!(input)
    IO.puts(JSON.encode!(Fixture.sum(a, b)))
end
"""
)


def run(argv, *, cwd):
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=180
    )


@unittest.skipUnless(NATIVE, "set LITERATE_AI_ELIXIR_QUALIFICATION=1 for native proof")
class ElixirNativeQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = discover_elixir_toolchain()
        expected = os.environ.get("LITAI_ELIXIR_EXPECT_VERSION")
        otp = os.environ.get("LITAI_ELIXIR_EXPECT_OTP")
        if expected and cls.tool.version != expected:
            raise AssertionError(f"expected Elixir {expected}, got {cls.tool.version}")
        if otp and cls.tool.otp_version != otp:
            raise AssertionError(f"expected OTP {otp}, got {cls.tool.otp_version}")

    def project(self, root, *, build_system=None, multiple=False):
        _, snapshot, execution = _locked_snapshot(
            root / "authority",
            language="elixir",
            platform=HOST,
            build_system=build_system,
            duplicate_entrypoint=multiple,
        )
        closure = project_locked_standard_toolchain_closure(
            snapshot, execution, host_platform=HOST, dependency_observer=_observation
        )
        source = root / "candidate with spaces"
        (source / "source/tests").mkdir(parents=True)
        (source / "source/main.exs").write_text(_MAIN, encoding="utf-8")
        (source / "source/helper.ex").write_text(_HELPER, encoding="utf-8")
        (source / "source/tests/litai_test.exs").write_text(_TESTS, encoding="utf-8")
        if multiple:
            (source / "source/run-again.exs").write_text(_MAIN, encoding="utf-8")
        artifacts = root / "artifacts with spaces"
        artifacts.mkdir()
        return closure, source, artifacts

    def argv(self, closure, contract, phase, source, artifacts):
        tool_id = contract.tool_binding(phase).toolchain_identity
        binding = next(
            item for item in closure.tool_bindings if item.toolchain_identity == tool_id
        )
        replacements = {
            "{source_root}": str(source),
            "{object_root}": str(artifacts.parent / "objects with spaces"),
            "{artifact_root}": str(artifacts),
            "{export_path}": str(artifacts / contract.artifact_export.export_id),
            "{provider_artifacts}": "[]",
        }
        argv = []
        for token in contract.command(phase).argv:
            argv.extend(
                binding.command
                if token == "{tool}"
                else (replacements.get(token, token),)
            )
        return argv

    def assert_modes(self, closure, source, artifacts):
        # Independent product requests are deliberately outside the native suite.
        for contract in closure.contracts:
            tested = run(
                self.argv(
                    closure, contract, ComponentCommandPhase.TEST, source, artifacts
                ),
                cwd=artifacts,
            )
            self.assertEqual(tested.returncode, 0, tested.stderr)
            self.assertEqual(
                json.loads(tested.stdout)["cases"],
                [{"case_id": "fixture-example", "outcome": "passed"}],
            )
            smoke = self.argv(
                closure, contract, ComponentCommandPhase.EXECUTE, source, artifacts
            )
            for arguments, expected in (
                (None, 42),
                ([9007199254740993, 17], 9007199254741010),
                ([-8, 3], -5),
            ):
                command = (
                    smoke if arguments is None else [*smoke[:-1], json.dumps(arguments)]
                )
                result = run(command, cwd=artifacts)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertTrue(result.stdout.endswith("\n"))

    def test_native_tree_and_multiple_entrypoints_survive_workspace_removal(self):
        for multiple in (False, True):
            with (
                self.subTest(multiple=multiple),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                closure, source, artifacts = self.project(root, multiple=multiple)
                built = run(
                    self.argv(
                        closure,
                        closure.contracts[0],
                        ComponentCommandPhase.BUILD,
                        source,
                        artifacts,
                    ),
                    cwd=root,
                )
                self.assertEqual(built.returncode, 0, built.stderr)
                shutil.rmtree(source)
                closure.require_unchanged()
                self.assert_modes(closure, source, artifacts)

    def test_json_transport_preserves_unicode_and_shell_characters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            closure, source, artifacts = self.project(root)
            (source / "source/main.exs").write_text(
                "IO.puts(JSON.encode!(JSON.decode!(hd(System.argv()))))\n",
                encoding="utf-8",
            )
            built = run(
                self.argv(
                    closure,
                    closure.contracts[0],
                    ComponentCommandPhase.BUILD,
                    source,
                    artifacts,
                ),
                cwd=root,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            shutil.rmtree(source)
            command = self.argv(
                closure,
                closure.contracts[0],
                ComponentCommandPhase.EXECUTE,
                source,
                artifacts,
            )
            request = [
                {"text": '%PATH% !LITAI! & | > < ^ " 😀 \ue000', "lines": "a\nb"}
            ]
            result = run(
                [*command[:-1], json.dumps(request, ensure_ascii=False)], cwd=artifacts
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), request)

    def test_real_parser_is_inert_and_refuses_bad_helpers_and_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            closure, source, artifacts = self.project(root)
            marker = root / "must-not-execute"
            helper = source / "source/helper.ex"
            helper.write_text(
                f'File.write!({json.dumps(str(marker))}, "executed")\n' + _HELPER,
                encoding="utf-8",
            )
            build = self.argv(
                closure,
                closure.contracts[0],
                ComponentCommandPhase.BUILD,
                source,
                artifacts,
            )
            self.assertEqual(run(build, cwd=root).returncode, 0)
            self.assertFalse(marker.exists())
            export = artifacts / closure.contracts[0].artifact_export.export_id
            for relative in ("source/helper.ex", "source/tests/litai_test.exs"):
                path = source / relative
                saved = path.read_text(encoding="utf-8")
                path.write_text("defmodule Broken do\n  def value(\n", encoding="utf-8")
                rejected = run(build, cwd=root)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse(export.exists())
                self.assertFalse(marker.exists())
                path.write_text(saved, encoding="utf-8")

    def test_native_test_failure_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            closure, source, artifacts = self.project(root)
            (source / "source/tests/litai_test.exs").write_text(
                'raise "deliberate native assertion failure"\n', encoding="utf-8"
            )
            built = run(
                self.argv(
                    closure,
                    closure.contracts[0],
                    ComponentCommandPhase.BUILD,
                    source,
                    artifacts,
                ),
                cwd=root,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            tested = run(
                self.argv(
                    closure,
                    closure.contracts[0],
                    ComponentCommandPhase.TEST,
                    source,
                    artifacts,
                ),
                cwd=root,
            )
            self.assertNotEqual(tested.returncode, 0)
            self.assertNotIn('"outcome":"passed"', tested.stdout)

    @unittest.skipUnless(
        BUILD_SYSTEMS, "enable real Make/Bazel qualification explicitly"
    )
    def test_make_exports_a_self_contained_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            closure, source, artifacts = self.project(root, build_system="make")
            (source / "source/main.exs").write_text(_SINGLE, encoding="utf-8")
            (source / "source/assemble.exs").write_text(
                "[input, output] = System.argv()\n"
                "Code.string_to_quoted!(File.read!(input))\n"
                "File.cp!(input, output)\n",
                encoding="utf-8",
            )
            (source / "source/Makefile").write_text(
                'all:\n\t"$(LITAI_LANGUAGE_TOOL)" assemble.exs '
                'main.exs "$(EXPORT_PATH)"\n',
                encoding="utf-8",
            )
            built = run(
                self.argv(
                    closure,
                    closure.contracts[0],
                    ComponentCommandPhase.BUILD,
                    source,
                    artifacts,
                ),
                cwd=root,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertTrue(
                (artifacts / closure.contracts[0].artifact_export.export_id).is_file()
            )
            shutil.rmtree(source)
            self.assert_modes(closure, source, artifacts)

    @unittest.skipUnless(
        BUILD_SYSTEMS, "enable real Make/Bazel qualification explicitly"
    )
    def test_bazel_exports_a_self_contained_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            closure, source, artifacts = self.project(root, build_system="bazel")
            (source / "source/main.exs").write_text(_SINGLE, encoding="utf-8")
            (source / "source/assemble.exs").write_text(
                "[input, output] = System.argv()\nFile.cp!(input, output)\n",
                encoding="utf-8",
            )
            (source / "MODULE.bazel").write_text(
                'module(name="elixir_qualification")\n', encoding="utf-8"
            )
            assemble = (
                '"$$LITAI_LANGUAGE_TOOL" $(location source/assemble.exs) '
                "$(location source/main.exs) $@"
            )
            (source / "BUILD.bazel").write_text(
                'genrule(name="litai_artifact", '
                'srcs=["source/main.exs", "source/assemble.exs"], '
                f'outs=["run.exs"], cmd={json.dumps(assemble)})\n',
                encoding="utf-8",
            )
            target = closure.bazel_targets[0]
            tool = next(
                binding
                for binding in closure.tool_bindings
                if binding.toolchain_identity == target.build_system_toolchain_identity
            )
            # Bazel produces its declared output; Standard's runtime consumes the
            # exported file, never a launcher pointing back at bazel-bin.
            options = [
                *tool.command,
                "--batch",
                f"--output_base={root / 'bazel-objects'}",
            ]
            built = run(
                [
                    *options,
                    "build",
                    "--symlink_prefix=/",
                    *target.build_options,
                    target.target_label,
                ],
                cwd=source,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            location = run([*options, "info", "bazel-bin"], cwd=source)
            self.assertEqual(location.returncode, 0, location.stderr)
            shutil.copy2(
                Path(location.stdout.strip()) / target.output_path,
                artifacts / closure.contracts[0].artifact_export.export_id,
            )
            shutil.rmtree(source)
            self.assert_modes(closure, source, artifacts)


if __name__ == "__main__":
    unittest.main()
