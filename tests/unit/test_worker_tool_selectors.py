"""Worker selector resolution preserves private search and invocation authority."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
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

    def test_lookup_never_executes_selector_and_refuses_shadowed_search(self):
        with (
            patch.dict(os.environ, {"PATH": str(self.shadow)}),
            patch("subprocess.Popen", side_effect=AssertionError("selector executed")),
        ):
            self.verify()
            with self.assertRaises(ActionWireError):
                self.verify((self.name, "different"))
            other = self.shadow / self.name
            other.write_bytes(b"different executable")
            other.chmod(0o755)
            self.environment["PATH"] = str(self.shadow) + os.pathsep + str(self.bin)
            with self.assertRaises(ActionWireError):
                self.verify()
        self.tool.require_unchanged.assert_called()
