from __future__ import annotations

import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from literate_ai.adapters.mcp_runtime import (
    StdioJsonRpcMcpTransport,
    UnreachableMcp,
    fan_out_author_event,
    set_mcp_transport_factory,
)
from literate_ai.contracts.channel_events import (
    ChannelEvent,
    ChannelKind,
    ChannelRole,
)
from literate_ai.contracts.projects import InstitutionalChannels
from literate_ai.contracts.user_mcp import (
    UserMcpCatalog,
    UserMcpServer,
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

    def test_stdio_transport_speaks_json_rpc_with_a_stub_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "stub.py"
            stub.write_text(_STUB_SERVER, encoding="utf-8")
            server = _server("jira", sys.executable, str(stub))
            transport = StdioJsonRpcMcpTransport()
            self.assertEqual(transport.list_tools(server), ("add_comment",))
            transport.call_tool(server, "add_comment", {"issue": "LAI-1", "body": "ok"})
