"""Worker selector resolution preserves private search and invocation authority."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.builders.cpp import CppToolchain
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.worker_tool_selectors import verify_worker_tool_selector


class WorkerToolSelectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / "bin"
        self.shadow = self.root / "shadow"
        self.bin.mkdir()
        self.shadow.mkdir()
        self.name = "tool.exe" if os.name == "nt" else "tool"
        self.executable = self.bin / self.name
        self.executable.write_bytes(b"not executed")
        self.executable.chmod(0o755)
        self.tool = SimpleNamespace(
            command=(str(self.executable), "fixed"),
            environment=(),
            require_unchanged=Mock(),
        )
        self.environment = {"PATH": str(self.bin), "PATHEXT": ".EXE;.CMD"}

    def verify(self, selector=None, **kwargs):
        verify_worker_tool_selector(
            self.tool,
            selector or (self.name, "fixed"),
            environment=self.environment,
            require_current=kwargs.get("guard", lambda: None),
        )

    def test_private_lookup_and_exact_arguments_never_execute_the_selector(self):
        with (
            patch.dict(os.environ, {"PATH": str(self.shadow)}),
            patch("subprocess.Popen", side_effect=AssertionError("selector executed")),
        ):
            self.verify()
            if os.name == "nt":
                self.verify(("tool", "fixed"))
            self.verify((str(self.executable), "fixed"))
            with self.assertRaises(ActionWireError):
                self.verify((self.name, "different"))
        self.tool.require_unchanged.assert_called()

    def test_missing_ambient_relative_and_shadowed_search_authority_refuses(self):
        for path in (None, "", ".", str(self.bin) + os.pathsep):
            if path is None:
                self.environment.pop("PATH", None)
            else:
                self.environment["PATH"] = path
            with self.subTest(path=path), self.assertRaises(ActionWireError):
                self.verify()
        self.environment["PATH"] = str(self.bin)
        for selector in (("./" + self.name, "fixed"), ("../" + self.name, "fixed")):
            with self.assertRaises(ActionWireError):
                self.verify(selector)
        other = self.shadow / self.name
        other.write_bytes(b"different executable")
        other.chmod(0o755)
        self.environment["PATH"] = str(self.shadow) + os.pathsep + str(self.bin)
        with self.assertRaises(ActionWireError):
            self.verify()

    def test_search_shadowing_between_checks_refuses(self):
        self.environment["PATH"] = str(self.shadow) + os.pathsep + str(self.bin)
        count = 0

        def guard():
            nonlocal count
            count += 1
            if count == 2:
                file = self.shadow / self.name
                file.write_bytes(b"shadow")
                file.chmod(0o755)

        with self.assertRaises(ActionWireError):
            self.verify(guard=guard)

    def test_only_canonicalizing_compiler_adapter_accepts_a_symlink_alias(self):
        alias = self.bin / ("alias.exe" if os.name == "nt" else "alias")
        try:
            alias.symlink_to(self.executable)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(ActionWireError):
            self.verify((alias.name, "fixed"))
        compiler = CppToolchain(
            self.tool.command, "gnu", "clang version 20.1.0", "sha256:" + "a" * 64
        )
        with patch.object(CppToolchain, "require_unchanged"):
            verify_worker_tool_selector(
                compiler,
                (alias.name, "fixed"),
                environment=self.environment,
                require_current=lambda: None,
            )

    def test_actual_configured_python_returns_exact_observed_tool(self):
        tool = discover_python_toolchain(pinned_command=(sys.executable,))
        binding = LocalComponentToolBinding.from_observed_toolchain(tool)
        worker = ConfiguredBuildWorker(
            binding,
            (binding,),
            environment={
                "PATH": str(Path(tool.command[0]).parent),
                "PATHEXT": ".EXE;.CMD",
            },
            standard_tools={"python": tool},
        )
        result = worker.verify_tool_selector(
            "python", (Path(tool.command[0]).name,), require_current=lambda: None
        )
        self.assertEqual(result.toolchain_identity.uri, tool.identity)
        self.assertEqual(result.command, tool.command)
        with self.assertRaises(ActionWireError):
            worker.verify_tool_selector(
                "missing", ("python",), require_current=lambda: None
            )
