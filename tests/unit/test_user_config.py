from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.user_config import (
    UserConfigError,
    catalog_setup_needed,
    ensure_user_mcp_catalog,
    journal_mutagenic_event,
    load_user_mcp_catalog,
    user_config_dir,
    write_user_mcp_catalog,
)
from literate_ai.cli.dispatch import main
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.channel_events import (
    CHANNEL_MARKER,
    ChannelEvent,
    ChannelKind,
    ChannelRole,
    is_mutagenic_command,
)
from literate_ai.contracts.user_mcp import (
    EMPTY_USER_MCP_CATALOG,
    UserMcpCatalog,
    UserMcpUses,
)


class UserMcpCatalogTests(unittest.TestCase):
    def test_empty_catalog_round_trips(self) -> None:
        catalog = UserMcpCatalog.from_dict(EMPTY_USER_MCP_CATALOG.to_dict())
        self.assertEqual(catalog.mcps, ())
        self.assertFalse(catalog.has(UserMcpUses.JIRA))

    def test_infers_jira_uses_from_id(self) -> None:
        catalog = UserMcpCatalog.from_dict(
            {
                "schema": UserMcpCatalog.SCHEMA,
                "mcps": [{"id": "jira"}],
            }
        )
        self.assertTrue(catalog.has(UserMcpUses.JIRA))
        self.assertEqual(catalog.mcps[0].uses, UserMcpUses.JIRA)

    def test_rejects_secret_shaped_channel_fields(self) -> None:
        with self.assertRaises(ContractValidationError):
            ChannelEvent.from_dict(
                {
                    "schema": ChannelEvent.SCHEMA,
                    "marker": CHANNEL_MARKER,
                    "role": "author",
                    "kind": "mutagenic-cli",
                    "project_id": "demo",
                    "user": "xoxb-1234567890-token",
                }
            )

    def test_trailer_round_trip(self) -> None:
        event = ChannelEvent(
            role=ChannelRole.AUTHOR,
            kind=ChannelKind.MUTAGENIC_CLI,
            project_id="literate-ai",
            command="release.publish",
            outcome="ok",
        )
        parsed = ChannelEvent.parse_trailer(event.render())
        self.assertEqual(parsed.role, ChannelRole.AUTHOR)
        self.assertEqual(parsed.command, "release.publish")
        self.assertIn("[literate-ai] author mutagenic-cli", event.first_line())

    def test_recipient_inbound_parses(self) -> None:
        event = ChannelEvent(
            role=ChannelRole.RECIPIENT,
            kind=ChannelKind.INBOUND_TASK,
            project_id="demo",
        )
        parsed = ChannelEvent.parse_trailer(event.render())
        self.assertEqual(parsed.role, ChannelRole.RECIPIENT)
        self.assertEqual(parsed.kind, ChannelKind.INBOUND_TASK)

    def test_lock_check_is_not_mutagenic(self) -> None:
        self.assertTrue(is_mutagenic_command("lock"))
        self.assertFalse(is_mutagenic_command("lock", lock_check=True))
        self.assertFalse(is_mutagenic_command("help"))
        self.assertFalse(is_mutagenic_command("release.plan"))
        self.assertFalse(is_mutagenic_command("catalog.graph"))
        self.assertTrue(is_mutagenic_command("catalog.copy"))
        self.assertTrue(is_mutagenic_command("flavor.add"))


class UserConfigAdapterTests(unittest.TestCase):
    def test_non_tty_does_not_write_when_setup_needed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            stderr = io.StringIO()
            catalog = ensure_user_mcp_catalog(
                json_mode=False,
                stdin=io.StringIO("y\n"),
                stderr=stderr,
                root=root,
            )
            self.assertEqual(catalog.mcps, ())
            self.assertFalse(root.exists())

    def test_decline_writes_empty_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            stdin = io.StringIO("n\n")
            stderr = io.StringIO()
            stdin.isatty = lambda: True  # type: ignore[method-assign]
            stderr.isatty = lambda: True  # type: ignore[method-assign]
            catalog = ensure_user_mcp_catalog(
                json_mode=False,
                stdin=stdin,
                stderr=stderr,
                root=root,
            )
            self.assertEqual(catalog.mcps, ())
            loaded = load_user_mcp_catalog(root)
            self.assertEqual(loaded.mcps, ())

    def test_accept_records_mcp_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            stdin = io.StringIO("y\njira, slack\n")
            stderr = io.StringIO()
            stdin.isatty = lambda: True  # type: ignore[method-assign]
            stderr.isatty = lambda: True  # type: ignore[method-assign]
            catalog = ensure_user_mcp_catalog(
                json_mode=False,
                stdin=stdin,
                stderr=stderr,
                root=root,
            )
            self.assertTrue(catalog.has(UserMcpUses.JIRA))
            self.assertTrue(catalog.has(UserMcpUses.SLACK))

    def test_journal_skips_without_catalog_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            self.assertIsNone(journal_mutagenic_event("init", outcome="ok", root=root))

    def test_journal_writes_author_event_when_catalog_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            write_user_mcp_catalog(EMPTY_USER_MCP_CATALOG, root)
            path = journal_mutagenic_event("init", outcome="ok", root=root)
            self.assertIsNotNone(path)
            assert path is not None
            event = ChannelEvent.from_dict(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(event.role, ChannelRole.AUTHOR)
            self.assertEqual(event.command, "init")

    def test_malformed_project_does_not_leak_partial_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "literate.project.json").write_text(
                '{"project_id":"leaked","version":7}', encoding="utf-8"
            )
            config = Path(directory) / "config"
            write_user_mcp_catalog(EMPTY_USER_MCP_CATALOG, config)
            with mock.patch(
                "literate_ai.adapters.user_config.Path.cwd", return_value=project
            ):
                path = journal_mutagenic_event("init", outcome="ok", root=config)
            assert path is not None
            event = ChannelEvent.from_dict(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(event.project_id, "")

    def test_config_dir_override_must_be_absolute(self) -> None:
        with mock.patch.dict(os.environ, {"LITAI_CONFIG_DIR": "relative"}, clear=False):
            with self.assertRaises(UserConfigError):
                user_config_dir()

    def test_help_does_not_prompt_or_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "litai"
            stdout = io.StringIO()
            stderr = io.StringIO()
            stdin = io.StringIO("n\n")
            stdin.isatty = lambda: True  # type: ignore[method-assign]
            stderr.isatty = lambda: True  # type: ignore[method-assign]
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"FORCE_COLOR", "COLORTERM", "CLICOLOR", "CLICOLOR_FORCE"}
            }
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"
            env["LITAI_CONFIG_DIR"] = str(root)
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("sys.stdin", stdin),
            ):
                status = main(("help",), stdout=stdout, stderr=stderr)
            self.assertEqual(status, 0)
            self.assertFalse(root.exists())
            self.assertNotIn("Set it up now", stderr.getvalue())


class CatalogSetupNeededTests(unittest.TestCase):
    def test_missing_directory_needs_setup(self) -> None:
        self.assertTrue(catalog_setup_needed(Path("/no/such/litai-config-dir")))

    def test_unrelated_user_assets_do_not_imply_mcp_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workers.json").write_text("{}", encoding="utf-8")

            self.assertTrue(catalog_setup_needed(root))
            self.assertEqual(load_user_mcp_catalog(root), EMPTY_USER_MCP_CATALOG)
