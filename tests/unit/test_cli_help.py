from __future__ import annotations

import argparse
import io
import json
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

    def test_lifecycle_commands_preserve_automatic_parallelism(self):
        parser = _parser()
        for command in ("build", "test", "rebuild", "profile"):
            with self.subTest(command=command):
                self.assertIsNone(parser.parse_args([command, "sample"]).jobs)
                self.assertEqual(
                    parser.parse_args([command, "sample", "--jobs", "3"]).jobs, 3
                )

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

    def test_top_level_help_is_cp1252_encodable(self) -> None:
        status, stdout, stderr = self.invoke("help")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        stdout.encode("cp1252")
        self.assertIn("spec-to-source maps", stdout)

    def test_help_resolves_top_level_and_nested_command_paths(self) -> None:
        for topic, expected in (
            (("init",), "usage: litai init"),
            (("reparent",), "usage: litai reparent"),
            (("project", "validate"), "usage: litai project validate"),
            (("project", "ci-plan"), "usage: litai project ci-plan"),
            (("project", "peer-work"), "usage: litai project peer-work"),
            (("project", "mac-contract"), "usage: litai project mac-contract"),
            (("spec", "qualify"), "usage: litai spec qualify"),
            (("design", "refine"), "usage: litai design refine"),
            (("release", "plan"), "usage: litai release plan"),
            (("release", "backport"), "usage: litai release backport"),
            (("release", "verify-published"), "usage: litai release verify-published"),
            (("package", "plan"), "usage: litai package plan"),
        ):
            with self.subTest(topic=topic):
                status, stdout, stderr = self.invoke("help", *topic)
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                self.assertTrue(stdout.startswith(expected))

    def test_trailing_help_verb_narrows_at_every_command_depth(self) -> None:
        for arguments, expected in (
            (("init", "help"), "usage: litai init"),
            (("reparent", "help"), "usage: litai reparent"),
            (("project", "help"), "usage: litai project"),
            (("project", "validate", "help"), "usage: litai project validate"),
            (("spec", "qualify", "help"), "usage: litai spec qualify"),
            (("release", "publish", "help"), "usage: litai release publish"),
            (("package", "plan", "help"), "usage: litai package plan"),
        ):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.invoke(*arguments)
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                self.assertTrue(stdout.startswith(expected))

        status, init_help, stderr = self.invoke("init", "help")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        normalized_init_help = " ".join(init_help.split())
        self.assertIn("existing README are preserved", normalized_init_help)
        self.assertIn("init --convert", normalized_init_help)
        self.assertIn("test-matrix examples", normalized_init_help)
        self.assertIn("platform-resolved user configuration", normalized_init_help)
        self.assertIn(
            "Python, GNU Make, the host operating system, and pip-wheel packaging",
            normalized_init_help,
        )
        self.assertIn(
            "bazel, cmake, make, repo.sh (build system)", normalized_init_help
        )
        self.assertIn("--from", normalized_init_help)
        self.assertIn("complete ancestor DAG", normalized_init_help)
        self.assertIn("--type", normalized_init_help)
        self.assertIn("application and full-stack", normalized_init_help)
        self.assertIn("--baseline-timeout-seconds", normalized_init_help)
        self.assertIn("1..86400; default: 1800", normalized_init_help)
        self.assertIn("--baseline-diagnostic-chars", normalized_init_help)
        self.assertIn("512..65536; default: 8192", normalized_init_help)

        parsed = _parser().parse_args(
            [
                "init",
                "--convert",
                "--run-baseline",
                "--baseline-timeout-seconds",
                "7200",
                "--baseline-diagnostic-chars",
                "16384",
                ".",
            ]
        )
        self.assertEqual(parsed.baseline_timeout_seconds, 7200)
        self.assertEqual(parsed.baseline_diagnostic_chars, 16384)

        for topic in (
            ("build",),
            ("test",),
            ("run",),
            ("package", "build"),
            ("package", "verify"),
        ):
            with self.subTest(worker_help=topic):
                status, lifecycle_help, stderr = self.invoke(*topic, "help")
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                if topic[0] != "package":
                    self.assertIn("--target", lifecycle_help)
                self.assertIn("--worker", lifecycle_help)
                self.assertIn("--worker-param", lifecycle_help)

        status, reparent_help, stderr = self.invoke("reparent", "help")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("literal 'none'", reparent_help)
        self.assertIn("divorce", reparent_help)
        self.assertIn("--apply", reparent_help)

        status, catalog_copy_help, stderr = self.invoke("catalog", "copy", "help")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("git:", catalog_copy_help)
        self.assertIn("#revision", catalog_copy_help)
        self.assertNotIn("git: not yet supported", catalog_copy_help)

        status, update_help, stderr = self.invoke("update", "help")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("every recorded repository ancestor", update_help)
        self.assertIn("--adopt-added", update_help)
        self.assertIn("--take-upstream", update_help)
        self.assertIn("--keep-local", update_help)
        self.assertIn("explicitly reviewed path", update_help)

    def test_trailing_help_preserves_global_json_mode(self) -> None:
        status, stdout, stderr = self.invoke("--json", "project", "validate", "help")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        result = json.loads(stdout)
        self.assertEqual(result["command"], "help")
        self.assertEqual(result["result"]["topic"], ["project", "validate"])

    def test_model_option_is_available_only_to_model_invoking_lifecycles(self) -> None:
        for verb in ("plan", "build", "test", "generate", "rebuild"):
            with self.subTest(verb=verb):
                status, command_help, stderr = self.invoke(verb, "help")
                self.assertEqual(status, 0)
                self.assertEqual(stderr, "")
                self.assertIn("--model", command_help)

        status, run_help, stderr = self.invoke("run", "help")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("--model", run_help)

    def test_rebuild_help_scopes_source_cache_overrides_to_the_external_driver(
        self,
    ) -> None:
        status, command_help, stderr = self.invoke("rebuild", "help")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("--source-cache-entry", command_help)
        self.assertIn("--source-cache-root", command_help)
        self.assertIn("external lifecycle driver only", command_help)
        self.assertIn("--from-accepted-source", command_help)

    def test_json_help_preserves_the_global_json_contract(self) -> None:
        status, stdout, stderr = self.invoke("--json", "help", "init")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        result = json.loads(stdout)
        self.assertEqual(result["command"], "help")
        self.assertEqual(result["result"]["schema"], "literate-ai/cli-help@1")
        self.assertEqual(result["result"]["topic"], ["init"])
        self.assertTrue(result["result"]["text"].startswith("usage: litai init"))

    def test_unknown_help_topic_is_a_stable_usage_failure(self) -> None:
        status, stdout, stderr = self.invoke("help", "does-not-exist")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        failure = json.loads(stderr)
        self.assertEqual(failure["error"]["code"], "cli.help_topic_unknown")

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
