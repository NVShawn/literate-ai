from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.catalog_import import catalog_copy
from literate_ai.adapters.project_mcp import (
    ProjectMcpError,
    assert_project_mcp_hygiene,
    parse_project_mcp_markdown,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.projects import InstitutionalChannels
from tests.support.fixtures_test_catalog_import import make_project

_VALID_MCP = b"""---
name: loan-risk-tools
version: 1.0.0
---
Project-owned MCP server for loan-risk queries.
"""


class ProjectMcpHygieneTests(unittest.TestCase):
    def test_parse_valid_sentinel(self) -> None:
        item = parse_project_mcp_markdown(_VALID_MCP, source="mcp.md")
        self.assertEqual(item.mcp_id, "loan-risk-tools")
        self.assertEqual(item.version, "1.0.0")

    def test_rejects_operator_reserved_id(self) -> None:
        with self.assertRaises(ProjectMcpError):
            parse_project_mcp_markdown(
                b"---\nname: jira\nversion: 1.0.0\n---\nNot a project MCP.\n",
                source="mcp.md",
            )

    def test_rejects_secret_in_body(self) -> None:
        with self.assertRaises(ProjectMcpError):
            parse_project_mcp_markdown(
                b"---\nname: demo-tools\nversion: 1.0.0\n---\nxoxb-1234567890-secret\n",
                source="mcp.md",
            )

    def test_duplicate_ids_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a" / "loan-risk-tools"
            second = root / "b" / "loan-risk-tools"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "mcp.md").write_bytes(_VALID_MCP)
            (second / "mcp.md").write_bytes(_VALID_MCP)
            with self.assertRaises(ProjectMcpError) as raised:
                assert_project_mcp_hygiene(
                    mcp_roots=(root / "a", root / "b"),
                    skill_roots=(),
                )
            self.assertEqual(raised.exception.code, "project_mcp.duplicate_id")

    def test_generation_skill_operator_mcp_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory) / "skills"
            target = skills / "specification-to-source" / "demo"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(
                "Call the Jira MCP during generation.\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProjectMcpError) as raised:
                assert_project_mcp_hygiene(mcp_roots=(), skill_roots=(skills,))
            self.assertEqual(raised.exception.code, "project_mcp.generation_prompt")

    def test_embedded_application_mcp_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory) / "skills"
            target = skills / "specification-to-source" / "service"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(
                "Expose this project's own MCP server as a product entrypoint.\n",
                encoding="utf-8",
            )
            items = assert_project_mcp_hygiene(mcp_roots=(), skill_roots=(skills,))
            self.assertEqual(items, ())

    def test_directory_name_must_match_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = Path(directory) / "wrong-name"
            item.mkdir()
            (item / "mcp.md").write_bytes(_VALID_MCP)
            with self.assertRaises(ProjectMcpError) as raised:
                assert_project_mcp_hygiene(mcp_roots=(Path(directory),), skill_roots=())
            self.assertEqual(raised.exception.code, "project_mcp.id_mismatch")

    def test_catalog_copy_pins_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = make_project(Path(directory) / "source", "proj-src")
            dest = make_project(Path(directory) / "dest", "proj-dst")
            mcp_dir = source / "mcps" / "loan-risk-tools"
            mcp_dir.mkdir(parents=True)
            (mcp_dir / "mcp.md").write_bytes(_VALID_MCP)
            catalog_copy(dest, str(source), ["mcp:loan-risk-tools"])
            copied = dest / "mcps" / "loan-risk-tools" / "mcp.md"
            self.assertTrue(copied.is_file())
            loaded = parse_project_mcp_markdown(
                copied.read_bytes(), source=copied.as_posix()
            )
            self.assertEqual(loaded.mcp_id, "loan-risk-tools")


class InstitutionalChannelsTests(unittest.TestCase):
    def test_rejects_webhook_url(self) -> None:
        with self.assertRaises(ContractValidationError):
            InstitutionalChannels.from_dict(
                {"slack_channel": "https://hooks.slack.com/services/T/B/x"}
            )

    def test_round_trip_slack_and_mail(self) -> None:
        channels = InstitutionalChannels.from_dict(
            {
                "slack_channel": "C0123456789",
                "outlook_from": "status@example.com",
                "outlook_to": ["team@example.com"],
                "jira_issue": "PROJ-12",
            }
        )
        self.assertEqual(
            InstitutionalChannels.from_dict(channels.to_dict()).jira_issue, "PROJ-12"
        )


class ChannelFormatterTests(unittest.TestCase):
    def test_jira_slack_outlook_share_trailer(self) -> None:
        from literate_ai.contracts.channel_events import (
            ChannelEvent,
            ChannelKind,
            ChannelRole,
        )

        event = ChannelEvent(
            role=ChannelRole.AUTHOR,
            kind=ChannelKind.MUTAGENIC_CLI,
            project_id="literate-ai",
            command="init",
            outcome="ok",
        )
        subject, body = event.as_outlook_message()
        self.assertEqual(subject, event.first_line())
        for payload in (event.as_jira_comment(), event.as_slack_message(), body):
            parsed = ChannelEvent.parse_trailer(payload)
            self.assertEqual(parsed.role, ChannelRole.AUTHOR)
            self.assertEqual(parsed.command, "init")

    def test_unmarked_text_is_not_admitted(self) -> None:
        from literate_ai.contracts.channel_events import ChannelEvent

        with self.assertRaises(ContractValidationError):
            ChannelEvent.parse_trailer("please bump the version")
