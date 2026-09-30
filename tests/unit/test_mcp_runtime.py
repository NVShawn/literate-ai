from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from literate_ai.adapters.mcp_runtime import (
    StdioJsonRpcMcpTransport,
    UnreachableMcp,
    discover_catalog_mcps,
    fan_out_author_event,
    load_project_channels,
    notify_tool_arguments,
    report_discovery,
    select_notify_tool,
    set_mcp_transport_factory,
)
from literate_ai.cli.dispatch import main
from literate_ai.contracts.channel_events import (
    ChannelEvent,
    ChannelKind,
    ChannelRole,
)
from literate_ai.contracts.project_mcp import generation_skill_requires_operator_mcp
from literate_ai.contracts.projects import InstitutionalChannels
from literate_ai.contracts.user_mcp import (
    UserMcpCatalog,
    UserMcpServer,
    UserMcpUses,
)

_ROOT = Path(__file__).resolve().parents[2]
_PYTHON_SERVICE_SKILL = (
    _ROOT
    / "skills"
    / "specification-to-source"
    / "backend-application"
    / "python-service-application"
    / "SKILL.md"
)
_STUB_SERVER = r"""
import json
import sys

def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", "replace").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    return json.loads(sys.stdin.buffer.read(length))

def write_msg(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()

while True:
    message = read_msg()
    if message is None:
        break
    method = message.get("method")
    message_id = message.get("id")
    if method == "initialize":
        write_msg(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stub", "version": "0"},
                },
            }
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        write_msg(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {"tools": [{"name": "add_comment"}]},
            }
        )
    elif method == "tools/call":
        write_msg({"jsonrpc": "2.0", "id": message_id, "result": {"content": []}})
"""


class FakeMcpTransport:
    def __init__(self, tools: dict[str, tuple[str, ...]] | None = None) -> None:
        self.listed: list[str] = []
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.tools = tools or {
            "jira": ("add_comment",),
            "slack": ("chat_postMessage",),
            "outlook": ("send_email",),
        }

    def list_tools(self, server: UserMcpServer) -> tuple[str, ...]:
        self.listed.append(server.id)
        names = self.tools.get(server.id, ())
        if not names:
            raise UnreachableMcp(f"{server.id}: no MCP transport")
        return names

    def call_tool(
        self, server: UserMcpServer, tool: str, arguments: Mapping[str, object]
    ) -> None:
        self.calls.append((server.id, tool, dict(arguments)))


def _catalog(*servers: UserMcpServer) -> UserMcpCatalog:
    return UserMcpCatalog(mcps=servers)


def _server(mcp_id: str, *command: str) -> UserMcpServer:
    return UserMcpServer.from_dict({"id": mcp_id, "command": list(command) or ["mcp"]})


def _event() -> ChannelEvent:
    return ChannelEvent(
        role=ChannelRole.AUTHOR,
        kind=ChannelKind.MUTAGENIC_CLI,
        project_id="literate-ai",
        command="init",
        outcome="ok",
    )


class McpRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_mcp_transport_factory(None)

    def test_malformed_project_does_not_leak_partial_channels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "literate.project.json").write_text(
                json.dumps(
                    {
                        "project_id": "demo",
                        "version": 7,
                        "institutional_channels": {"jira_issue": "SECRET-1"},
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(load_project_channels(root))

    def test_selects_notify_tools_by_channel(self) -> None:
        self.assertEqual(
            select_notify_tool(UserMcpUses.JIRA, ("add_comment", "search")),
            "add_comment",
        )
        self.assertEqual(
            select_notify_tool(UserMcpUses.JIRA, ("jira_search", "jira_update_issue")),
            "jira_update_issue",
        )
        self.assertEqual(
            select_notify_tool(UserMcpUses.SLACK, ("chat_postMessage",)),
            "chat_postMessage",
        )
        self.assertIsNone(select_notify_tool(UserMcpUses.JIRA, ("search",)))

    def test_discovery_off_does_not_probe(self) -> None:
        fake = FakeMcpTransport()
        ids = discover_catalog_mcps(
            _catalog(_server("jira", "npx", "jira")),
            enabled=False,
            transport=fake,
        )
        self.assertEqual(ids, ())
        self.assertEqual(fake.listed, [])

    def test_discovery_on_probes_servers_with_command(self) -> None:
        fake = FakeMcpTransport()
        ids = discover_catalog_mcps(
            _catalog(
                _server("jira", "npx", "jira"),
                UserMcpServer.from_dict({"id": "slack"}),
            ),
            enabled=True,
            transport=fake,
        )
        self.assertEqual(ids, ("jira",))
        self.assertEqual(fake.listed, ["jira"])

    def test_fan_out_posts_listed_channels(self) -> None:
        fake = FakeMcpTransport()
        skips = fan_out_author_event(
            _event(),
            _catalog(
                _server("jira", "npx", "jira"),
                _server("slack", "npx", "slack"),
                _server("outlook", "npx", "outlook"),
            ),
            InstitutionalChannels.from_dict(
                {
                    "jira_issue": "LAI-1",
                    "slack_channel": "#project-updates",
                    "outlook_to": ["ops@example.com"],
                }
            ),
            transport=fake,
        )
        self.assertEqual(skips, ())
        posted = {item[0] for item in fake.calls}
        self.assertEqual(posted, {"jira", "slack", "outlook"})
        jira = next(item for item in fake.calls if item[0] == "jira")
        self.assertEqual(jira[2]["issue"], "LAI-1")

    def test_fan_out_maps_jira_update_issue_to_a_comment_add(self) -> None:
        fake = FakeMcpTransport(tools={"jira": ("jira_update_issue",)})
        skips = fan_out_author_event(
            _event(),
            _catalog(_server("jira", "npx", "-y", "mcp-remote")),
            InstitutionalChannels.from_dict({"jira_issue": "EXAMPLE-123"}),
            transport=fake,
        )
        self.assertEqual(skips, ())
        self.assertEqual(fake.calls[0][1], "jira_update_issue")
        self.assertEqual(
            fake.calls[0][2],
            notify_tool_arguments(
                UserMcpUses.JIRA,
                "jira_update_issue",
                "EXAMPLE-123",
                _event().as_jira_comment(),
            ),
        )
        self.assertEqual(fake.calls[0][2]["issue_id"], "EXAMPLE-123")
        self.assertEqual(
            fake.calls[0][2]["update"]["comment"][0]["add"]["body"],
            _event().as_jira_comment(),
        )

    def test_fan_out_skips_missing_command_or_tool(self) -> None:
        fake = FakeMcpTransport(
            tools={"jira": ("search",), "slack": ("list_channels",)}
        )
        skips = fan_out_author_event(
            _event(),
            _catalog(
                UserMcpServer.from_dict({"id": "jira"}),
                _server("slack", "npx", "slack"),
            ),
            InstitutionalChannels.from_dict(
                {"jira_issue": "LAI-1", "slack_channel": "#alerts"}
            ),
            transport=fake,
            stderr=io.StringIO(),
        )
        self.assertIn("jira: catalog command missing", skips)
        self.assertIn("slack: no matching notify tool", skips)
        self.assertEqual(fake.calls, [])

    def test_fan_out_skips_unregistered_institutional_channel(self) -> None:
        fake = FakeMcpTransport()
        skips = fan_out_author_event(
            _event(),
            _catalog(_server("jira", "npx", "jira"), _server("slack", "npx", "slack")),
            InstitutionalChannels.from_dict({"jira_issue": "LAI-1"}),
            transport=fake,
            stderr=io.StringIO(),
        )
        self.assertEqual([item[0] for item in fake.calls], ["jira"])
        self.assertIn("slack: unregistered institutional channel", skips)

    def test_report_discovery_names_reachable_ids(self) -> None:
        stderr = io.StringIO()
        report_discovery(("jira",), enabled=True, stderr=stderr)
        self.assertIn("jira", stderr.getvalue())
        silent = io.StringIO()
        report_discovery(("jira",), enabled=False, stderr=silent)
        self.assertEqual(silent.getvalue(), "")

    def test_stdio_transport_speaks_json_rpc_with_a_stub_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "stub.py"
            stub.write_text(_STUB_SERVER, encoding="utf-8")
            server = _server("jira", sys.executable, str(stub))
            transport = StdioJsonRpcMcpTransport()
            self.assertEqual(transport.list_tools(server), ("add_comment",))
            transport.call_tool(server, "add_comment", {"issue": "LAI-1", "body": "ok"})

    def test_generation_skill_does_not_require_operator_mcp(self) -> None:
        text = _PYTHON_SERVICE_SKILL.read_text(encoding="utf-8")
        self.assertFalse(generation_skill_requires_operator_mcp(text))
        self.assertIn("lang-python", text)
        self.assertIn("modelcontextprotocol/python-sdk", text)
        self.assertIn("lang-javascript", text)
        self.assertIn("modelcontextprotocol/typescript-sdk", text)
        self.assertIn("lang-cpp", text)
        self.assertIn("hkr04/cpp-mcp", text)


class McpCliFlagTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_mcp_transport_factory(None)

    def _invoke(self, *arguments: str, root: Path) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"FORCE_COLOR", "COLORTERM", "CLICOLOR", "CLICOLOR_FORCE"}
        }
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        env["LITAI_CONFIG_DIR"] = str(root)
        with mock.patch.dict(os.environ, env, clear=True):
            status = main(arguments, stdout=stdout, stderr=stderr)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_help_lists_discover_mcps_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            status, stdout, stderr = self._invoke("help", root=root)
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("--discover-mcps", stdout)

    def test_discover_flag_off_does_not_probe(self) -> None:
        fake = FakeMcpTransport()
        set_mcp_transport_factory(lambda: fake)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            root.mkdir()
            (root / "mcps.json").write_text(
                json.dumps(
                    _catalog(_server("jira", "npx", "jira")).to_dict(),
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            status, _stdout, stderr = self._invoke("help", root=root)
        self.assertEqual(status, 0)
        self.assertEqual(fake.listed, [])
        self.assertNotIn("MCP discovery", stderr)

    def test_discover_flag_on_probes(self) -> None:
        fake = FakeMcpTransport()
        set_mcp_transport_factory(lambda: fake)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            root.mkdir()
            (root / "mcps.json").write_text(
                json.dumps(
                    _catalog(_server("jira", "npx", "jira")).to_dict(),
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            status, _stdout, stderr = self._invoke("--discover-mcps", "help", root=root)
        self.assertEqual(status, 0)
        self.assertEqual(fake.listed, ["jira"])
        self.assertIn("MCP discovery reachable: jira", stderr)
