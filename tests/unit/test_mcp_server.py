"""MCP stdio adapter wraps in-process CLI envelopes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.mcp_server import _TOOLS, _handle
from literate_ai.version import DISTRIBUTION_VERSION


class McpServerTests(unittest.TestCase):
    def test_mutations_are_annotated_and_require_explicit_acknowledgement(self) -> None:
        listed = {item["name"]: item for item in _TOOLS}
        self.assertTrue(listed["verify"]["annotations"]["readOnlyHint"])
        self.assertTrue(listed["work_close"]["annotations"]["destructiveHint"])
        for acknowledgement in (None, False, "true"):
            arguments = {"work_id": "EXAMPLE-001"}
            if acknowledgement is not None:
                arguments["acknowledge"] = acknowledgement
            with mock.patch("literate_ai.mcp_server._run_cli") as dispatch:
                result = _handle(
                    {
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "work_close",
                            "arguments": arguments,
                        },
                    }
                )
            self.assertEqual(result["error"]["code"], -32602)
            dispatch.assert_not_called()

    def test_paths_and_symlinks_cannot_escape_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for selection in (str(root.parent), "../outside"):
                with mock.patch("literate_ai.mcp_server._run_cli") as dispatch:
                    result = _handle(
                        {
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "verify",
                                "arguments": {"project": selection},
                            },
                        },
                        project_root=root,
                    )
                self.assertIn("mcp.path_outside_root", result["error"]["message"])
                dispatch.assert_not_called()
            try:
                (root / "escape").symlink_to(root.parent, target_is_directory=True)
            except OSError:
                self.skipTest("host does not permit directory symlinks")
            result = _handle(
                {
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "lock",
                        "arguments": {"component": "escape/other", "acknowledge": True},
                    },
                },
                project_root=root,
            )
            self.assertIn("mcp.path_outside_root", result["error"]["message"])

    def test_initialize_and_tools_list(self) -> None:
        initialized = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(
            initialized["result"]["serverInfo"]["version"], DISTRIBUTION_VERSION
        )
        listed = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {item["name"] for item in listed["result"]["tools"]}
        self.assertEqual(
            names,
            {item["name"] for item in _TOOLS},
        )
        self.assertIn("catalog_copy", names)
        self.assertIn("project_validate", names)
        self.assertIn("resources", initialized["result"]["capabilities"])
