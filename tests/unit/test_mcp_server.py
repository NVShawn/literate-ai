"""MCP stdio adapter wraps in-process CLI envelopes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.mcp_server import _TOOLS, _handle
from literate_ai.version import DISTRIBUTION_VERSION


class McpServerTests(unittest.TestCase):
    def test_ancestor_project_discovery_cannot_escape_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for method, params in (
                ("tools/call", {"name": "verify", "arguments": {}}),
                ("resources/list", {}),
                ("resources/read", {"uri": "litai://skill/literate-ai"}),
            ):
                with (
                    mock.patch(
                        "literate_ai.mcp_server.discover_project",
                        return_value=SimpleNamespace(root=root.parent),
                    ),
                    mock.patch("literate_ai.mcp_server._run_cli") as dispatch,
                    mock.patch(
                        "literate_ai.mcp_server.project_mcp_resources"
                    ) as resources,
                ):
                    result = _handle(
                        {"id": 1, "method": method, "params": params}, project_root=root
                    )
                self.assertIn("mcp.path_outside_root", result["error"]["message"])
                dispatch.assert_not_called()
                resources.assert_not_called()

    def test_tool_data_cannot_inject_cli_options_or_falsey_nonobjects(self) -> None:
        for arguments in (
            {"source": ".", "items": ["--project=/outside"], "acknowledge": True},
            {"source": ".", "items": ["--overwrite"], "acknowledge": True},
            [],
            False,
            "",
            None,
        ):
            with mock.patch("literate_ai.mcp_server._run_cli") as dispatch:
                result = _handle(
                    {
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "catalog_copy", "arguments": arguments},
                    }
                )
            self.assertEqual(result["error"]["code"], -32602)
            dispatch.assert_not_called()

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

    def test_paths_are_resolved_inside_session_root_before_dispatch(self) -> None:
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

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
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

    def test_verify_uses_public_cli_shape_and_reports_failed_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            response = {"ok": True, "result": {"ok": False}}
            with mock.patch(
                "literate_ai.mcp_server._run_cli",
                return_value={
                    "status": 1,
                    "envelope": response,
                },
            ) as dispatch:
                result = _handle(
                    {
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "verify",
                            "arguments": {},
                        },
                    },
                    project_root=root,
                )
            dispatch.assert_called_once_with(["verify", str(root)])
            self.assertTrue(result["result"]["isError"])
            self.assertEqual(
                json.loads(result["result"]["content"][0]["text"]), response
            )

    def test_global_configuration_write_requires_startup_opt_in(self) -> None:
        with mock.patch("literate_ai.mcp_server._run_cli") as dispatch:
            result = _handle(
                {
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "operator_mcp_write",
                        "arguments": {"servers": [], "acknowledge": True},
                    },
                }
            )
        self.assertIn("mcp.operator_config_disabled", result["error"]["message"])
        dispatch.assert_not_called()

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

    def test_skill_documentation_and_static_resources_are_listed_and_read(self) -> None:
        listed = _handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        resources = listed["result"]["resources"]
        uris = {item["uri"] for item in resources}
        self.assertIn("litai://skill/literate-ai", uris)
        self.assertIn("litai://documentation/docs/README.md", uris)
        self.assertIn("litai://static/project-template/SKILL.md", uris)

        read = _handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": "litai://skill/configure-test-workers"},
            }
        )
        content = read["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/markdown")
        self.assertIn("# Configure test workers", content["text"])
