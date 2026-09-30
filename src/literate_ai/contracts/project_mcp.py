"""Project-owned MCP catalog items (ADR 0024). Distinct from operator-local opt-in."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ._validation import fail, fields, string_value
from .channel_events import reject_secret_shaped
from .versioning import semantic_version

PROJECT_MCP_SENTINEL = "mcp.md"
OPERATOR_RESERVED_MCP_IDS = frozenset({"jira", "slack", "outlook", "registry"})
_MCP_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_OPERATOR_MCP_IN_GENERATION = re.compile(
    r"(?i)(~/\.config/(?:litai|literate-ai)|\boperator[- ]local mcp\b|"
    r"\b(jira|slack|outlook)\s+mcp\b)"
)


def project_mcp_id(value: Any, path: str) -> str:
    text = string_value(value, path, max_length=63)
    if _MCP_ID.fullmatch(text) is None:
        fail(path, "must be a lower-case id matching [a-z][a-z0-9-]{0,62}")
    if text in OPERATOR_RESERVED_MCP_IDS:
        fail(
            path,
            "must not reuse an operator-local MCP id as project catalog policy",
        )
    return text


@dataclass(frozen=True, slots=True)
class ProjectMcpMarkdown:
    """One project-owned MCP server described by directory-owned mcp.md."""

    mcp_id: str
    version: str
    body: str
    source: str

    @classmethod
    def from_frontmatter(
        cls,
        frontmatter: dict[str, Any],
        body: str,
        *,
        source: str,
    ) -> ProjectMcpMarkdown:
        data = fields(
            frontmatter,
            path=source,
            required=frozenset({"name", "version"}),
            optional=frozenset({"description"}),
        )
        mcp_id = project_mcp_id(data["name"], f"{source}.name")
        version = semantic_version(data["version"], f"{source}.version")
        reject_secret_shaped(body, f"{source}.body")
        if "description" in data:
            reject_secret_shaped(
                string_value(data["description"], f"{source}.description"),
                f"{source}.description",
            )
        return cls(mcp_id=mcp_id, version=version, body=body, source=source)


def generation_skill_requires_operator_mcp(text: str) -> bool:
    """True when a generation recipe requires an operator MCP."""

    return _OPERATOR_MCP_IN_GENERATION.search(text) is not None
