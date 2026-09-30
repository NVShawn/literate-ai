"""Shared Jira/Slack/Outlook channel envelope (ADR 0022, ADR 0023)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    string_value,
)

CHANNEL_EVENT_SCHEMA = "urn:literate-ai:schema:v1:channel-event"
CHANNEL_MARKER = "literate-ai"
CHANNEL_TRAILER_PREFIX = "literate-ai-event:1"
_SECRET_SHAPED = re.compile(
    r"(xox[baprs]-|sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]{20,})",
    re.IGNORECASE,
)

MUTAGENIC_COMMANDS = frozenset(
    {
        "init",
        "update",
        "reparent",
        "catalog.copy",
        "flavor.add",
        "lock",
        "generate",
        "rebuild",
        "release.prepare",
        "release.publish",
        "release.advance-default-branch",
    }
)


class ChannelRole(StrEnum):
    AUTHOR = "author"
    RECIPIENT = "recipient"


class ChannelKind(StrEnum):
    MUTAGENIC_CLI = "mutagenic-cli"
    INBOUND_TASK = "inbound-task"
    PUBLISH_SUMMARY = "publish-summary"


def reject_secret_shaped(value: str, path: str) -> str:
    if _SECRET_SHAPED.search(value):
        fail(path, "must not contain token-shaped bytes")
    return value


def is_mutagenic_command(
    command: str,
    *,
    lock_check: bool = False,
) -> bool:
    if command == "lock" and lock_check:
        return False
    return command in MUTAGENIC_COMMANDS


@dataclass(frozen=True, slots=True)
class ChannelEvent:
    """One institutional channel payload. Literate AI is author or recipient."""

    SCHEMA = CHANNEL_EVENT_SCHEMA

    role: ChannelRole
    kind: ChannelKind
    project_id: str
    command: str = ""
    revision: str = ""
    outcome: str = ""
    user: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "marker": CHANNEL_MARKER,
            "role": self.role.value,
            "kind": self.kind.value,
            "project_id": self.project_id,
            "command": self.command,
            "revision": self.revision,
            "outcome": self.outcome,
            "user": self.user,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "ChannelEvent") -> ChannelEvent:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"marker", "role", "kind", "project_id"}),
            optional=frozenset({"command", "revision", "outcome", "user"}),
        )
        marker = string_value(data["marker"], f"{path}.marker")
        if marker != CHANNEL_MARKER:
            fail(f"{path}.marker", f"must be {CHANNEL_MARKER!r}")
        project_id = reject_secret_shaped(
            string_value(data["project_id"], f"{path}.project_id", nonempty=False),
            f"{path}.project_id",
        )
        command = reject_secret_shaped(
            string_value(data.get("command", ""), f"{path}.command", nonempty=False),
            f"{path}.command",
        )
        revision = reject_secret_shaped(
            string_value(data.get("revision", ""), f"{path}.revision", nonempty=False),
            f"{path}.revision",
        )
        outcome = reject_secret_shaped(
            string_value(data.get("outcome", ""), f"{path}.outcome", nonempty=False),
            f"{path}.outcome",
        )
        user = reject_secret_shaped(
            string_value(data.get("user", ""), f"{path}.user", nonempty=False),
            f"{path}.user",
        )
        return cls(
            role=enum_value(ChannelRole, data["role"], f"{path}.role"),
            kind=enum_value(ChannelKind, data["kind"], f"{path}.kind"),
            project_id=project_id,
            command=command,
            revision=revision,
            outcome=outcome,
            user=user,
        )

    def first_line(self) -> str:
        parts = [
            f"[{CHANNEL_MARKER}]",
            self.role.value,
            self.kind.value,
            self.project_id or "-",
        ]
        if self.command:
            parts.append(self.command)
        if self.outcome:
            parts.append(self.outcome)
        return " ".join(parts)

    def trailer(self) -> str:
        fields = [
            f"role={self.role.value}",
            f"kind={self.kind.value}",
            f"project={self.project_id}",
        ]
        if self.command:
            fields.append(f"command={self.command}")
        if self.revision:
            fields.append(f"revision={self.revision}")
        if self.outcome:
            fields.append(f"outcome={self.outcome}")
        if self.user:
            fields.append(f"user={self.user}")
        return CHANNEL_TRAILER_PREFIX + " " + " ".join(fields)

    def render(self) -> str:
        return f"{self.first_line()}\n{self.trailer()}\n"

    def as_jira_comment(self) -> str:
        """Jira comment body. Same envelope as Slack and Outlook; no channel prefix."""

        return self.render()

    def as_slack_message(self) -> str:
        """Slack post body. Same envelope as Jira and Outlook; no channel prefix."""

        return self.render()

    def as_outlook_message(self) -> tuple[str, str]:
        """Outlook subject and body. Subject is the human first line."""

        return self.first_line(), self.render()

    @classmethod
    def parse_trailer(cls, text: str, *, path: str = "ChannelEvent") -> ChannelEvent:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(CHANNEL_TRAILER_PREFIX + " "):
                return cls._from_trailer(stripped, path=path)
        fail(path, "missing literate-ai-event trailer")

    @classmethod
    def _from_trailer(cls, line: str, *, path: str) -> ChannelEvent:
        body = line[len(CHANNEL_TRAILER_PREFIX) :].strip()
        parsed: dict[str, str] = {}
        for token in body.split():
            if "=" not in token:
                fail(path, "trailer tokens must be key=value")
            key, value = token.split("=", 1)
            parsed[key] = value
        document = {
            "schema": cls.SCHEMA,
            "marker": CHANNEL_MARKER,
            "role": parsed.get("role", ""),
            "kind": parsed.get("kind", ""),
            "project_id": parsed.get("project", ""),
            "command": parsed.get("command", ""),
            "revision": parsed.get("revision", ""),
            "outcome": parsed.get("outcome", ""),
            "user": parsed.get("user", ""),
        }
        return cls.from_dict(document, path=path)
