"""Public filesystem composition-root tests for Standard locked projects."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.standard_project import (
    _STANDARD_BUILD_DRIVER,
    _STANDARD_NODE_RUNTIME_DRIVER,
    _STANDARD_PYTHON_RUNTIME_DRIVER,
    _encoded_toolchain_environment,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    ComponentLifecycleCommand,
)


class StandardProjectFactoryTests(unittest.TestCase):
    def test_cpp_build_driver_compiles_every_translation_unit_with_source_include(
        self,
    ) -> None:
        compiler = next(
            (
                command
                for name in ("c++", "g++", "clang++")
                if (command := shutil.which(name)) is not None
            ),
            None,
        )
        if compiler is None:
            self.skipTest("a portable C++ compiler is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            source = root / "source"
            tests = source / "tests"
            tests.mkdir(parents=True)
            (source / "application.hpp").write_text(
                "#pragma once\nint application_value();\nint generated_tests();\n",
                encoding="utf-8",
            )
            (source / "application.cpp").write_text(
                '#include "application.hpp"\nint application_value() { return 7; }\n',
                encoding="utf-8",
            )
            (tests / "litai_test.cpp").write_text(
                '#include "application.hpp"\n'
                "int generated_tests() { return application_value() == 7 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            (source / "main.cpp").write_text(
                '#include "application.hpp"\n'
                "int main() { return generated_tests(); }\n",
                encoding="utf-8",
            )
            objects = Path(temporary) / "objects"
            executable = Path(temporary) / "artifact" / "run"

            subprocess.run(
                (
                    sys.executable,
                    "-c",
                    _STANDARD_BUILD_DRIVER,
                    "cpp-executable",
                    json.dumps([compiler]),
                    _encoded_toolchain_environment(()),
                    str(root),
                    "source/main.cpp",
                    str(objects),
                    str(executable),
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            completed = subprocess.run(
                (str(executable),),
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0)

    def test_node_runtime_driver_executes_artifact_as_main_program(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        lifecycle_command = ComponentLifecycleCommand(
            ComponentCommandPhase.TEST,
            (
                "{tool}",
                "-e",
                _STANDARD_NODE_RUNTIME_DRIVER,
                "{artifact_root}",
                "{export_path}",
                "tree",
                "source/main.js",
                "--litai-test",
            ),
        )
        self.assertIn("{artifact_root}", lifecycle_command.argv)
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "artifact"
            source = artifact / "source"
            source.mkdir(parents=True)
            (source / "main.js").write_text(
                "if (require.main !== module) throw new Error('not main');\n"
                "process.stdout.write(JSON.stringify("
                "{argv: process.argv.slice(2)}));\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                (
                    node,
                    "-e",
                    _STANDARD_NODE_RUNTIME_DRIVER,
                    str(artifact),
                    str(artifact),
                    "tree",
                    "source/main.js",
                    "--litai-test",
                ),
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertEqual(completed.stdout, '{"argv":["--litai-test"]}')

    def test_python_runtime_driver_imports_artifact_local_support_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "artifact"
            source = artifact / "source"
            support = source / "support"
            support.mkdir(parents=True)
            (support / "__init__.py").write_text(
                "VALUE = 'artifact-local'\n", encoding="utf-8"
            )
            (source / "main.py").write_text(
                "from support import VALUE\nprint(VALUE)\n", encoding="utf-8"
            )

            completed = subprocess.run(
                (
                    sys.executable,
                    "-c",
                    _STANDARD_PYTHON_RUNTIME_DRIVER,
                    str(artifact),
                    str(artifact),
                    "tree",
                    "source/main.py",
                ),
                text=True,
                capture_output=True,
                check=True,
            )

            artifact_files = tuple(
                path.relative_to(artifact).as_posix()
                for path in sorted(artifact.rglob("*"))
                if path.is_file()
            )

        self.assertEqual(completed.stdout.strip(), "artifact-local")
        self.assertEqual(
            artifact_files,
            ("source/main.py", "source/support/__init__.py"),
        )


if __name__ == "__main__":
    unittest.main()
