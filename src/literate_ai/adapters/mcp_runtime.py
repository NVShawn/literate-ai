"""Operator-catalog MCP client for mutagenic fan-out and opt-in discovery (ADR 0025).

The official Python module is ``mcp`` (modelcontextprotocol/python-sdk, MIT).
It is an optional extra (``literate-ai[mcp]``) so the wheel stays small. This
adapter speaks MCP over stdio JSON-RPC using catalog ``command`` argv; tests
inject a fake transport and never require a live Jira, Slack, or Outlook server.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Protocol, TextIO

from literate_ai.adapters._processes import (
    ProcessTreeOwnership,
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.contracts.channel_events import ChannelEvent
from literate_ai.contracts.projects import InstitutionalChannels
from literate_ai.contracts.user_mcp import UserMcpCatalog, UserMcpServer, UserMcpUses
from literate_ai.projects import ProjectConfigurationStore, ProjectError

_SPAWN_TIMEOUT_SECONDS = 8.0
_MCP_PROTOCOL_VERSION = "2024-11-05"


def load_project_channels(project: Path | None = None) -> InstitutionalChannels | None:
    try:
        definition = ProjectConfigurationStore(project or Path(".")).read().definition
    except ProjectError:
        return None
    return definition.institutional_channels


_TOOL_HINTS: dict[UserMcpUses, re.Pattern[str]] = {
    UserMcpUses.JIRA: re.compile(r"comment|jira_update_issue", re.IGNORECASE),
    UserMcpUses.SLACK: re.compile(
        r"post_message|chat_post|send_message", re.IGNORECASE
    ),
    UserMcpUses.OUTLOOK: re.compile(
        r"send_mail|send_email|create_message", re.IGNORECASE
    ),
}
_JIRA_UPDATE_ISSUE = re.compile(r"jira_update_issue", re.IGNORECASE)


class McpTransport(Protocol):
    def list_tools(self, server: UserMcpServer) -> tuple[str, ...]: ...

    def call_tool(
        self, server: UserMcpServer, tool: str, arguments: Mapping[str, object]
    ) -> None: ...


class UnreachableMcp(Exception):
    """A catalog server could not be probed or called. Fail-open at the CLI."""


class NullMcpTransport:
    """Used under tests, when discovery is off, or a server has no spawn command."""

    def list_tools(self, server: UserMcpServer) -> tuple[str, ...]:
        raise UnreachableMcp(f"{server.id}: no MCP transport")

    def call_tool(
        self, server: UserMcpServer, tool: str, arguments: Mapping[str, object]
    ) -> None:
        raise UnreachableMcp(f"{server.id}: no MCP transport")


class StdioJsonRpcMcpTransport:
    """Bounded stdio JSON-RPC client for catalog ``command`` argv."""

    def list_tools(self, server: UserMcpServer) -> tuple[str, ...]:
        with _StdioMcpSession(server) as session:
            return session.list_tools()

    def call_tool(
        self, server: UserMcpServer, tool: str, arguments: Mapping[str, object]
    ) -> None:
        with _StdioMcpSession(server) as session:
            session.call_tool(tool, arguments)


class _StdioMcpSession:
    def __init__(self, server: UserMcpServer) -> None:
        if not server.command:
            raise UnreachableMcp(f"{server.id}: catalog command missing")
        self._server = server
        self._proc: subprocess.Popen[bytes] | None = None
        self._ownership: ProcessTreeOwnership | None = None
        self._next_id = 1
        self._deadline = 0.0

    def __enter__(self) -> _StdioMcpSession:
        ownership = create_process_tree_ownership()
        try:
            self._proc = subprocess.Popen(
                list(self._server.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **ownership.popen_options,
            )
        except OSError as exc:
            ownership.release()
            raise UnreachableMcp(f"{self._server.id}: {exc.strerror or exc}") from exc
        ownership.bind(self._proc.pid)
        self._ownership = ownership
        self._deadline = time.monotonic() + _SPAWN_TIMEOUT_SECONDS
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "litai", "version": "0.8.0"},
                },
            )
            self._notify("notifications/initialized")
        except UnreachableMcp:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        proc = self._proc
        ownership = self._ownership
        self._proc = None
        self._ownership = None
        if proc is None:
            if ownership is not None:
                ownership.release()
            return
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            terminate_process_tree(proc, ownership=ownership)
            with suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=1)
        finally:
            if ownership is not None:
                ownership.release()

    def list_tools(self) -> tuple[str, ...]:
        result = self._request("tools/list")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise UnreachableMcp(f"{self._server.id}: tools/list returned no tools")
        names: list[str] = []
        for item in tools:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
        return tuple(names)

    def call_tool(self, tool: str, arguments: Mapping[str, object]) -> None:
        self._request("tools/call", {"name": tool, "arguments": dict(arguments)})

    def _notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def _request(
        self, method: str, params: Mapping[str, object] | None = None
    ) -> object:
        message_id = self._next_id
        self._next_id += 1
        payload: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = dict(params)
        self._write(payload)
        response = self._read()
        if not isinstance(response, dict):
            raise UnreachableMcp(
                f"{self._server.id}: {method} returned no JSON-RPC object"
            )
        if response.get("id") != message_id:
            raise UnreachableMcp(
                f"{self._server.id}: {method} returned a mismatched id"
            )
        if "error" in response:
            raise UnreachableMcp(f"{self._server.id}: {method} error")
        return response.get("result")

    def _write(self, payload: Mapping[str, object]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise UnreachableMcp(f"{self._server.id}: MCP process is not running")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            proc.stdin.write(header + body)
            proc.stdin.flush()
        except OSError as exc:
            raise UnreachableMcp(f"{self._server.id}: {exc.strerror or exc}") from exc

    def _read(self) -> object:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise UnreachableMcp(f"{self._server.id}: MCP process is not running")
        try:
            headers: dict[str, str] = {}
            while True:
                line = self._read_bytes_until(lambda stdout: stdout.readline())
                if not line:
                    raise UnreachableMcp(f"{self._server.id}: MCP stdout closed")
                if line in (b"\r\n", b"\n"):
                    break
                decoded = line.decode("ascii", errors="replace")
                key, _, value = decoded.partition(":")
                headers[key.strip().lower()] = value.strip()
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError as exc:
                raise UnreachableMcp(
                    f"{self._server.id}: invalid Content-Length"
                ) from exc
            if length <= 0 or length > 1_000_000:
                raise UnreachableMcp(f"{self._server.id}: invalid Content-Length")
            body = self._read_bytes_until(lambda stdout: stdout.read(length))
        except OSError as exc:
            raise UnreachableMcp(f"{self._server.id}: {exc.strerror or exc}") from exc
        if len(body) != length:
            raise UnreachableMcp(f"{self._server.id}: truncated MCP response")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise UnreachableMcp(
                f"{self._server.id}: MCP response is not JSON"
            ) from exc

    def _read_bytes_until(self, reader: Callable[[object], bytes]) -> bytes:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise UnreachableMcp(f"{self._server.id}: MCP process is not running")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise UnreachableMcp(f"{self._server.id}: MCP timed out")
        box: list[bytes] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                box.append(reader(proc.stdout))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(remaining)
        if thread.is_alive():
            raise UnreachableMcp(f"{self._server.id}: MCP timed out")
        if errors:
            raise UnreachableMcp(f"{self._server.id}: {errors[0]}") from errors[0]
        return box[0] if box else b""


def select_notify_tool(uses: UserMcpUses, tools: Sequence[str]) -> str | None:
    hint = _TOOL_HINTS.get(uses)
    if hint is None:
        return None
    for name in tools:
        if hint.search(name):
            return name
    return None


def notify_tool_arguments(
    uses: UserMcpUses,
    tool: str,
    destination: str,
    body: str,
) -> dict[str, object]:
    """Map one author envelope onto the selected notify tool's arguments."""

    if uses is UserMcpUses.JIRA and _JIRA_UPDATE_ISSUE.search(tool):
        return {
            "issue_id": destination,
            "update": {"comment": [{"add": {"body": body}}]},
        }
    arguments: dict[str, object] = {"body": body, "text": body}
    if uses is UserMcpUses.JIRA:
        arguments["issue"] = destination
    elif uses is UserMcpUses.SLACK:
        arguments["channel"] = destination
    elif uses is UserMcpUses.OUTLOOK:
        arguments["to"] = destination
    return arguments


def discover_catalog_mcps(
    catalog: UserMcpCatalog,
    *,
    enabled: bool,
    transport: McpTransport,
) -> tuple[str, ...]:
    """Return reachable catalog ids. Does nothing when discovery is off."""

    if not enabled:
        return ()
    reachable: list[str] = []
    for server in catalog.mcps:
        if not server.command:
            continue
        try:
            transport.list_tools(server)
        except UnreachableMcp:
            continue
        reachable.append(server.id)
    return tuple(reachable)


def fan_out_author_event(
    event: ChannelEvent,
    catalog: UserMcpCatalog,
    channels: InstitutionalChannels | None,
    *,
    transport: McpTransport,
    stderr: TextIO | None = None,
) -> tuple[str, ...]:
    """Post one author envelope. Returns skip reasons. Never raises to the CLI."""

    skips: list[str] = []
    destinations = (
        (
            UserMcpUses.JIRA,
            (channels.jira_issue if channels else ""),
            event.as_jira_comment(),
        ),
        (
            UserMcpUses.SLACK,
            (channels.slack_channel if channels else ""),
            event.as_slack_message(),
        ),
        (
            UserMcpUses.OUTLOOK,
            ",".join(channels.outlook_to) if channels and channels.outlook_to else "",
            event.as_outlook_message()[1],
        ),
    )
    for uses, destination, body in destinations:
        if not catalog.has(uses):
            continue
        if uses is not UserMcpUses.JIRA and not destination:
            skips.append(f"{uses.value}: unregistered institutional channel")
            continue
        if uses is UserMcpUses.JIRA and not destination:
            skips.append("jira: no institutional_channels.jira_issue")
            continue
        server = next(
            item for item in catalog.mcps if item.uses is uses or item.id == uses.value
        )
        if not server.command:
            skips.append(f"{uses.value}: catalog command missing")
            continue
        try:
            tools = transport.list_tools(server)
            tool = select_notify_tool(uses, tools)
            if tool is None:
                skips.append(f"{uses.value}: no matching notify tool")
                continue
            transport.call_tool(
                server,
                tool,
                notify_tool_arguments(uses, tool, destination, body),
            )
        except UnreachableMcp as exc:
            skips.append(str(exc))
    if skips and stderr is not None:
        stderr.write("Literate AI skipped MCP notify (" + "; ".join(skips) + ").\n")
    return tuple(skips)


def report_discovery(
    ids: Sequence[str],
    *,
    enabled: bool,
    stderr: TextIO | None,
) -> None:
    if stderr is None or not enabled:
        return
    if not ids:
        stderr.write("Literate AI MCP discovery found no reachable catalog commands.\n")
        return
    stderr.write("Literate AI MCP discovery reachable: " + ", ".join(ids) + ".\n")


def default_transport() -> McpTransport:
    """Stdio in production; Null under pytest so unit tests never spawn MCPs."""

    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("LITAI_MCP_TRANSPORT") == "off"
    ):
        return NullMcpTransport()
    return StdioJsonRpcMcpTransport()


_TRANSPORT_FACTORY: Callable[[], McpTransport] = default_transport


def set_mcp_transport_factory(factory: Callable[[], McpTransport] | None) -> None:
    global _TRANSPORT_FACTORY
    _TRANSPORT_FACTORY = default_transport if factory is None else factory


def active_mcp_transport() -> McpTransport:
    return _TRANSPORT_FACTORY()
