from __future__ import annotations

import argparse
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.cli.dispatch import _parser, main

# Python 3.14 argparse colorizes help and honors FORCE_COLOR/COLORTERM even when
# help is written to a non-terminal stream. Several developer and CI shells export
# those, which would otherwise wrap this output in ANSI escapes and fail every
# exact-text assertion below for reasons unrelated to the CLI.
_PLAIN_TEXT_ENVIRONMENT = {
    key: value
    for key, value in os.environ.items()
    if key not in {"FORCE_COLOR", "COLORTERM", "CLICOLOR", "CLICOLOR_FORCE"}
} | {"NO_COLOR": "1", "TERM": "dumb"}


class CliHelpTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, _PLAIN_TEXT_ENVIRONMENT, clear=True):
            status = main(arguments, stdout=stdout, stderr=stderr)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_help_lists_the_discoverable_top_level_commands(self) -> None:
        status, stdout, stderr = self.invoke("help")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout.startswith("usage: litai "))
        self.assertIn("help", stdout)
        self.assertIn("init", stdout)
        self.assertIn("package", stdout)
        self.assertIn("design", stdout)
        self.assertIn("release", stdout)
        self.assertIn("reparent", stdout)
        self.assertIn("rebuild", stdout)

    def test_command_reference_lists_every_public_cli_verb_once(self) -> None:
        reference = (
            Path(__file__).resolve().parents[2] / "docs/user/configuration-and-cli.md"
        )
        text = reference.read_text(encoding="utf-8")
        start = text.index("```text\n") + len("```text\n")
        end = text.index("```", start)
        documented = []
        for line in text[start:end].splitlines():
            raw = line.strip()
            if not raw.startswith("litai"):
                continue
            tokens: list[str] = []
            for token in raw.split()[1:]:
                if (
                    token.startswith("[")
                    or token.startswith("--")
                    or "PATH" in token
                    or token.startswith("URL")
                ):
                    break
                tokens.append(token)
            documented.append("litai" + ((" " + " ".join(tokens)) if tokens else ""))
        self.assertEqual(len(documented), len(set(documented)))
        self.assertNotIn("litai worker capability", documented)

        def subcommands(
            parser: argparse.ArgumentParser,
        ) -> tuple[dict[str, argparse.ArgumentParser] | None, bool]:
            for action in parser._actions:
                choices = getattr(action, "choices", None)
                if (
                    isinstance(choices, dict)
                    and choices
                    and all(
                        isinstance(value, argparse.ArgumentParser)
                        for value in choices.values()
                    )
                ):
                    return choices, bool(getattr(action, "required", False))
            return None, False

        def leaves(
            parser: argparse.ArgumentParser, prefix: tuple[str, ...]
        ) -> list[str]:
            children, required = subcommands(parser)
            if children is None:
                return ["litai " + " ".join(prefix)] if prefix else []
            paths: list[str] = []
            if prefix and not required:
                paths.append("litai " + " ".join(prefix))
            for name, child in sorted(children.items()):
                paths.extend(leaves(child, (*prefix, name)))
            return paths

        shipped = leaves(_parser(), ())
        missing = [item for item in shipped if item not in documented]
        extra = [
            item
            for item in documented
            if item not in shipped and item not in {"litai graph rebalance"}
        ]
        self.assertEqual(missing, [])
        self.assertEqual(extra, [])
        for line in documented:
            topic = line.split()[1:]
            if line == "litai graph rebalance":
                topic = ["graph"]
            status, stdout, stderr = self.invoke("help", *topic)
            self.assertEqual(status, 0, line)
            self.assertEqual(stderr, "")
            self.assertTrue(stdout.startswith("usage: litai "))


if __name__ == "__main__":
    unittest.main()
