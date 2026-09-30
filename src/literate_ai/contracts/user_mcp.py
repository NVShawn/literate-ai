"""Operator-local MCP catalog contract (ADR 0021)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    list_value,
    string_tuple,
    string_value,
    unique,
)

USER_MCP_CATALOG_SCHEMA = "urn:literate-ai:schema:v1:user-mcp-catalog"
_MCP_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class UserMcpUses(StrEnum):
    JIRA = "jira"
    SLACK = "slack"
    OUTLOOK = "outlook"
    REGISTRY = "registry"
    OTHER = "other"


def _mcp_id(value: Any, path: str) -> str:
    text = string_value(value, path, max_length=63)
    if _MCP_ID.fullmatch(text) is None:
        fail(path, "must be a lower-case id matching [a-z][a-z0-9-]{0,62}")
    return text


@dataclass(frozen=True, slots=True)
class UserMcpServer:
    """One operator-opted MCP server. Command argv is optional launch hint only."""

    id: str
    uses: UserMcpUses
    command: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {"id": self.id, "uses": self.uses.value}
        if self.command:
            document["command"] = list(self.command)
        return document

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "UserMcpServer") -> UserMcpServer:
        data = fields(
            value,
            path=path,
            required=frozenset({"id"}),
            optional=frozenset({"uses", "command"}),
        )
        mcp_id = _mcp_id(data["id"], f"{path}.id")
        uses = (
            enum_value(UserMcpUses, data["uses"], f"{path}.uses")
            if "uses" in data
            else _uses_from_id(mcp_id)
        )
        command = (
            string_tuple(data["command"], f"{path}.command")
            if "command" in data
            else ()
        )
        if any(not item.strip() for item in command):
            fail(f"{path}.command", "must not contain empty strings")
        return cls(id=mcp_id, uses=uses, command=command)


def _uses_from_id(mcp_id: str) -> UserMcpUses:
    try:
        return UserMcpUses(mcp_id)
    except ValueError:
        return UserMcpUses.OTHER


@dataclass(frozen=True, slots=True)
class UserMcpCatalog:
    """Operator-local list of MCP servers this user wants Literate AI to use."""

    SCHEMA = USER_MCP_CATALOG_SCHEMA

    mcps: tuple[UserMcpServer, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "mcps": [item.to_dict() for item in self.mcps],
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "UserMcpCatalog") -> UserMcpCatalog:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"mcps"}),
        )
        servers = tuple(
            UserMcpServer.from_dict(item, path=f"{path}.mcps[{index}]")
            for index, item in enumerate(list_value(data["mcps"], f"{path}.mcps"))
        )
        unique(tuple(item.id for item in servers), f"{path}.mcps", "mcp ids")
        return cls(mcps=servers)

    def has(self, uses: UserMcpUses) -> bool:
        return any(item.uses is uses or item.id == uses.value for item in self.mcps)


EMPTY_USER_MCP_CATALOG = UserMcpCatalog(mcps=())
