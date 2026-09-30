"""Cross-platform proof for lock-derived Standard commands and host closure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.lifecycle import local_generated_source_tree_identity
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts import ComponentCommandPhase, ComponentCommandRole
from tests.unit.test_standard_command_projection import _locked_snapshot, _observation

_APPLICATION = """\
import json
import sys


def main(arguments):
    return {"argument_count": len(arguments), "status": "ok"}


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "--litai-test":
        print(json.dumps({
            "schema": "literate-ai/generated-test-results@1",
            "cases": [
                {"case_id": "boundary-empty", "outcome": "passed"},
                {"case_id": "example-one", "outcome": "passed"},
                {"case_id": "invariant-count", "outcome": "passed"},
            ],
        }, sort_keys=True, separators=(",", ":")))
    elif mode == "--litai-smoke":
        result = main([{"sample": True}])
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(main(json.loads(mode)), sort_keys=True, separators=(",", ":")))
"""

_LANGUAGE_APPLICATIONS = {
    "javascript": """\
const mode = process.argv[2];
if (mode === "--litai-test") {
  console.log('{"schema":"literate-ai/generated-test-results@1","cases":[{"case_id":"javascript-case","outcome":"passed"}]}');
} else {
  console.log('{"language":"javascript","status":"ok"}');
}
""",
    "rust": r"""
use std::env;

fn main() {
    let mode = env::args().nth(1).unwrap_or_default();
    if mode == "--litai-test" {
        println!(r#"{{"schema":"literate-ai/generated-test-results@1","cases":[{{"case_id":"rust-case","outcome":"passed"}}]}}"#);
    } else {
        println!(r#"{{"language":"rust","status":"ok"}}"#);
    }
}
""",
    "cpp": r"""
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    const std::string mode = argc > 1 ? argv[1] : "";
    if (mode == "--litai-test") {
        std::cout
            << R"({"schema":"literate-ai/generated-test-results@1",)"
            << R"("cases":[{"case_id":"cpp-case","outcome":"passed"}]})"
            << '\n';
    } else {
        std::cout << R"({"language":"cpp","status":"ok"})" << '\n';
    }
    return 0;
}
""",
}


def _host_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform in {"win32", "cygwin"}:
        return "windows"
    raise unittest.SkipTest(f"unsupported conformance host: {sys.platform}")


class StandardLockedCommandProjectionConformanceTests(unittest.TestCase):
    def test_real_python_closure_builds_tests_and_executes_projected_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform = _host_platform()
            _component, snapshot, execution = _locked_snapshot(
                root / "authority", platform=platform
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot, execution, host_platform=platform
            )
            contract = closure.contracts[0]
            binding_by_identity = {
                item.toolchain_identity.uri: item for item in closure.tool_bindings
            }
            source = root / "generated-source"
            (source / "source/tests").mkdir(parents=True)
            (source / "source/main.py").write_text(_APPLICATION, encoding="utf-8")
            (source / "source/tests/litai_test.py").write_text(
                "# Native cases are compiled into the application protocol.\n",
                encoding="utf-8",
            )
            source_identity = local_generated_source_tree_identity(source)
            objects = root / "objects"
            artifact = root / "artifact"
            objects.mkdir()
            artifact.mkdir()
            export = artifact / contract.artifact_export.export_id

            outputs = {}
            for phase in ComponentCommandPhase:
                command = contract.command(phase)
                binding = binding_by_identity[
                    contract.tool_binding(phase).toolchain_identity.uri
                ]
                values = {
                    ComponentCommandRole.TOOL: binding.command,
                    ComponentCommandRole.SOURCE_ROOT: (str(source),),
                    ComponentCommandRole.OBJECT_ROOT: (str(objects),),
                    ComponentCommandRole.ARTIFACT_ROOT: (str(artifact),),
                    ComponentCommandRole.EXPORT_PATH: (str(export),),
                    ComponentCommandRole.PROVIDER_ARTIFACTS: (),
                }
                argv = command.substitute(
                    {role: values[role] for role in command.roles}
                )
                outputs[phase] = subprocess.run(
                    argv,
                    cwd=artifact,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(
                    outputs[phase].returncode,
                    0,
                    (
                        f"phase={phase.value}\n"
                        f"argv={argv!r}\n"
                        f"stdout={outputs[phase].stdout}\n"
                        f"stderr={outputs[phase].stderr}"
                    ),
                )

            self.assertEqual(
                local_generated_source_tree_identity(source), source_identity
            )
            self.assertTrue((export / "source/main.py").is_file())
            test_result = json.loads(outputs[ComponentCommandPhase.TEST].stdout)
            self.assertEqual(
                {item["case_id"] for item in test_result["cases"]},
                {"boundary-empty", "example-one", "invariant-count"},
            )
            self.assertEqual(
                json.loads(outputs[ComponentCommandPhase.EXECUTE].stdout),
                {"argument_count": 1, "status": "ok"},
            )
            self.assertGreaterEqual(len(closure.dependency_observation.components), 1)
            self.assertGreaterEqual(len(closure.dependency_observation.edges), 1)
            closure.require_unchanged()

    def _assert_real_toolchain_projection(self, language: str) -> None:
        extensions = {"javascript": "js", "rust": "rs", "cpp": "cpp"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            platform = _host_platform()
            _component, snapshot, execution = _locked_snapshot(
                root / "authority", language=language, platform=platform
            )
            try:
                closure = project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform=platform,
                    dependency_observer=_observation,
                )
            except BuildError as error:
                if error.code.endswith("_toolchain_unavailable"):
                    raise unittest.SkipTest(str(error)) from error
                raise
            contract = closure.contracts[0]
            binding_by_identity = {
                item.toolchain_identity.uri: item for item in closure.tool_bindings
            }
            source = root / "generated-source"
            (source / "source/tests").mkdir(parents=True)
            extension = extensions[language]
            (source / f"source/main.{extension}").write_text(
                _LANGUAGE_APPLICATIONS[language], encoding="utf-8"
            )
            (source / f"source/tests/litai_test.{extension}").write_text(
                "// Generated cases are linked or dispatched by main.\n",
                encoding="utf-8",
            )
            source_identity = local_generated_source_tree_identity(source)
            objects = root / "objects"
            artifact = root / "artifact"
            objects.mkdir()
            artifact.mkdir()
            export = artifact / contract.artifact_export.export_id
            outputs = {}
            for phase in ComponentCommandPhase:
                command = contract.command(phase)
                binding = binding_by_identity[
                    contract.tool_binding(phase).toolchain_identity.uri
                ]
                values = {
                    ComponentCommandRole.TOOL: binding.command,
                    ComponentCommandRole.SOURCE_ROOT: (str(source),),
                    ComponentCommandRole.OBJECT_ROOT: (str(objects),),
                    ComponentCommandRole.ARTIFACT_ROOT: (str(artifact),),
                    ComponentCommandRole.EXPORT_PATH: (str(export),),
                    ComponentCommandRole.PROVIDER_ARTIFACTS: (),
                }
                argv = command.substitute(
                    {role: values[role] for role in command.roles}
                )
                outputs[phase] = subprocess.run(
                    argv,
                    cwd=artifact,
                    env={**os.environ, **dict(binding.environment)},
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(
                    outputs[phase].returncode,
                    0,
                    (
                        f"phase={phase.value}\n"
                        f"argv={argv!r}\n"
                        f"stdout={outputs[phase].stdout}\n"
                        f"stderr={outputs[phase].stderr}"
                    ),
                )
            self.assertEqual(
                local_generated_source_tree_identity(source), source_identity
            )
            self.assertTrue(export.is_file() or export.is_dir())
            self.assertEqual(
                json.loads(outputs[ComponentCommandPhase.TEST].stdout)["cases"],
                [{"case_id": f"{language}-case", "outcome": "passed"}],
            )
            self.assertEqual(
                json.loads(outputs[ComponentCommandPhase.EXECUTE].stdout),
                {"language": language, "status": "ok"},
            )
            closure.require_unchanged()

    def test_javascript_toolchain_compiles_and_runs_projected_artifact(self) -> None:
        self._assert_real_toolchain_projection("javascript")

    def test_rust_toolchain_compiles_and_runs_projected_artifact(self) -> None:
        self._assert_real_toolchain_projection("rust")

    def test_cpp_toolchain_compiles_and_runs_projected_artifact(self) -> None:
        self._assert_real_toolchain_projection("cpp")


if __name__ == "__main__":
    unittest.main()
