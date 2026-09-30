"""Persistent-service process projection for Standard runtime drivers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_NATIVE_RUNTIME_DRIVER,
    STANDARD_NODE_RUNTIME_DRIVER,
    STANDARD_PYTHON_RUNTIME_DRIVER,
    direct_service_process_argv,
)


class StandardRuntimeServiceCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_python_file_driver_becomes_the_owned_entrypoint(self) -> None:
        entrypoint = self.root / "service.py"
        entrypoint.write_bytes(b"pass\n")

        result = direct_service_process_argv(
            (
                "python",
                "-I",
                "-c",
                STANDARD_PYTHON_RUNTIME_DRIVER,
                str(self.root),
                str(entrypoint),
                "file",
                "source/main.py",
                "--litai-smoke",
            )
        )

        self.assertEqual(result, ("python", "-I", str(entrypoint), "--litai-serve"))

    def test_javascript_tree_driver_becomes_the_owned_entrypoint(self) -> None:
        export = self.root / "artifact"
        entrypoint = export / "source" / "main.js"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_bytes(b"// service\n")

        result = direct_service_process_argv(
            (
                "node",
                "-e",
                STANDARD_NODE_RUNTIME_DRIVER,
                str(self.root),
                str(export),
                "tree",
                "source/main.js",
                "--litai-smoke",
            )
        )

        self.assertEqual(result, ("node", str(entrypoint), "--litai-serve"))

    def test_native_driver_executes_the_artifact_without_python_wrapper(self) -> None:
        entrypoint = self.root / "service"
        entrypoint.write_bytes(b"native\n")

        result = direct_service_process_argv(
            (
                "python",
                "-c",
                STANDARD_NATIVE_RUNTIME_DRIVER,
                str(self.root),
                str(entrypoint),
                "--litai-smoke",
            )
        )

        self.assertEqual(result, (str(entrypoint), "--litai-serve"))

    def test_direct_command_retains_smoke_to_serve_compatibility(self) -> None:
        self.assertEqual(
            direct_service_process_argv(("service", "--litai-smoke")),
            ("service", "--litai-serve"),
        )

    def test_missing_or_unsafe_standard_entrypoint_fails_closed(self) -> None:
        missing = self.root / "missing.js"
        command = (
            "node",
            "-e",
            STANDARD_NODE_RUNTIME_DRIVER,
            str(self.root),
            str(missing),
            "file",
            "source/main.js",
            "--litai-smoke",
        )
        with self.assertRaisesRegex(ValueError, "regular packaged file"):
            direct_service_process_argv(command)

        target = self.root / "target.js"
        target.write_bytes(b"// service\n")
        link = self.root / "link.js"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            return
        with self.assertRaisesRegex(ValueError, "regular packaged file"):
            direct_service_process_argv((*command[:-4], str(link), *command[-3:]))

    def test_tree_entrypoint_rejects_nonportable_or_linked_paths(self) -> None:
        export = self.root / "artifact"
        export.mkdir()
        command = (
            "node",
            "-e",
            STANDARD_NODE_RUNTIME_DRIVER,
            str(self.root),
            str(export),
            "tree",
            "..\\outside.js",
            "--litai-smoke",
        )
        with self.assertRaisesRegex(ValueError, "canonical POSIX separators"):
            direct_service_process_argv(command)

        target = self.root / "target"
        target.mkdir()
        linked = export / "linked"
        try:
            linked.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        (target / "service.js").write_bytes(b"// service\n")
        with self.assertRaisesRegex(ValueError, "contains a link"):
            direct_service_process_argv(
                (*command[:-2], "linked/service.js", command[-1])
            )


if __name__ == "__main__":
    unittest.main()
