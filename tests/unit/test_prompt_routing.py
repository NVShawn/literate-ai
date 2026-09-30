"""Direct prompt-master product wiring fails closed on MAC envelopes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.application.prompt_routing import (
    MAC_TASK_ENVIRONMENT,
    PromptRoutingError,
    translate_direct_prompt,
)
from literate_ai.cli.prompt import prompt_translate_from_args


class PromptRoutingTests(unittest.TestCase):
    def test_direct_request_becomes_a_bounded_task_envelope(self) -> None:
        envelope = translate_direct_prompt(
            "  add a library kind contract  ",
            environment={},
            coding_cli="codex",
        )
        self.assertEqual(envelope["schema"], "literate-ai/prompt-task@1")
        self.assertEqual(envelope["translation"], "prompt-master")
        self.assertEqual(envelope["provider"], "codex")
        self.assertEqual(envelope["outcome"], "add a library kind contract")
        self.assertIn("Outcome", envelope["task"])
        self.assertIn("Do not rewrite locked", envelope["task"])
        self.assertFalse(envelope["mac_bypass"])

    def test_mac_envelope_and_empty_requests_fail_closed(self) -> None:
        with self.assertRaises(PromptRoutingError) as mac:
            translate_direct_prompt(
                "implement the Component",
                environment={MAC_TASK_ENVIRONMENT: "mac-task-1"},
            )
        self.assertEqual(mac.exception.code, "prompt_routing.mac_envelope_bypass")
        with self.assertRaises(PromptRoutingError) as empty:
            translate_direct_prompt("   ", environment={})
        self.assertEqual(empty.exception.code, "prompt_routing.request_empty")
        with self.assertRaises(PromptRoutingError) as flagged:
            translate_direct_prompt(
                "implement the Component",
                environment={},
                mac_envelope=True,
            )
        self.assertEqual(flagged.exception.code, "prompt_routing.mac_envelope_bypass")

    def test_cli_translate_wraps_the_application_envelope(self) -> None:
        args = mock.Mock(
            request=("lock", "the", "library", "kind"),
            file=None,
            project=".",
            component=None,
            coding_cli="claude",
            mac_envelope=False,
        )
        envelope = prompt_translate_from_args(args)
        self.assertEqual(envelope["outcome"], "lock the library kind")
        self.assertEqual(envelope["provider"], "claude")

    def test_cli_mac_flag_is_typed(self) -> None:
        from literate_ai.cli.errors import CliFailure

        args = mock.Mock(
            request=("do", "work"),
            file=None,
            project=".",
            component=None,
            coding_cli=None,
            mac_envelope=True,
        )
        with self.assertRaises(CliFailure) as raised:
            prompt_translate_from_args(args)
        self.assertEqual(raised.exception.code, "prompt_routing.mac_envelope_bypass")

    def test_cli_reads_a_request_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request.txt"
            path.write_text("pin live qualification\n", encoding="utf-8")
            args = mock.Mock(
                request=(),
                file=str(path),
                project=".",
                component=None,
                coding_cli=None,
                mac_envelope=False,
            )
            envelope = prompt_translate_from_args(args)
        self.assertEqual(envelope["outcome"], "pin live qualification")


if __name__ == "__main__":
    unittest.main()
