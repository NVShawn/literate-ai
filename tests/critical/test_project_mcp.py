from __future__ import annotations

import unittest

from literate_ai.adapters.project_mcp import (
    ProjectMcpError,
    parse_project_mcp_markdown,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.projects import InstitutionalChannels


class ProjectMcpHygieneTests(unittest.TestCase):
    def test_rejects_secret_in_body(self) -> None:
        with self.assertRaises(ProjectMcpError):
            parse_project_mcp_markdown(
                b"---\nname: demo-tools\nversion: 1.0.0\n---\nxoxb-1234567890-secret\n",
                source="mcp.md",
            )


class InstitutionalChannelsTests(unittest.TestCase):
    def test_rejects_webhook_url(self) -> None:
        with self.assertRaises(ContractValidationError):
            InstitutionalChannels.from_dict(
                {"slack_channel": "https://hooks.slack.com/services/T/B/x"}
            )
