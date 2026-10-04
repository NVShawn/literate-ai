"""Public rendering/cache behavior over real project JSON and emitted HTML bytes.

Only installation discovery is a declared synthetic fixture here. A separately
installed non-editable wheel must qualify that final operator boundary.
"""

import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_render
from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.cli import dispatch
from literate_ai.contracts.html_observability import HtmlRenderResult
from tests.support import fixtures_test_html_observability_schema as schema_tests
from tests.support.fixtures_test_html_emitter import DISTRIBUTION, ROOT, request


def _project(root):
    (root / ".literate").mkdir()
    definition = json.loads((ROOT / "literate.project.json").read_text())
    for field in ("component_roots", "flavor_roots", "workflow_roots", "routing_roots"):
        definition[field] = []
    (root / "literate.project.json").write_text(json.dumps(definition))
    for name in ("repository-parent.json", "repository-lineage.json"):
        (root / ".literate" / name).write_bytes(
            (ROOT / ".literate" / name).read_bytes()
        )
    (root / "SKILL.md").write_text(
        "---\nname: example\ndescription: Test project instructions.\n---\n"
    )
    skill = root / "skills/agent/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example-agent\ndescription: Nested test.\n---\n")


class HtmlRenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        _project(self.root)
        self.request = replace(
            request("literate-ai", policy="pinned-cdn"), cache_mode="read-write"
        )
        self.output = self.root / "graph.html"
        self.framework = HtmlFrameworkObservation(DISTRIBUTION, "1.1.0")
        patch = mock.patch.object(
            html_render, "observe_html_framework", return_value=self.framework
        )
        self.observer = patch.start()
        self.addCleanup(patch.stop)
        environment = mock.patch.dict(
            os.environ, {"BUILD_DIR": "generated", "OBJ_DIR": "objects"}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def render(self, **changes):
        return html_render.render_html(self.root, replace(self.request, **changes))

    def assert_refused(self, result, code):
        self.assertEqual(result.status, "refused", result)
        self.assertIsNone(result.artifact)
        self.assertEqual(result.refusal.code, code)

    def test_foreign_or_edited_output_is_preserved_without_cache_creation(self):
        for content in (b"", b"<html>my handwritten report</html>"):
            with self.subTest(content=content):
                self.output.write_bytes(content)
                self.assert_refused(self.render(), "render.cache_write_forbidden")
                self.assertEqual(self.output.read_bytes(), content)
                self.assertFalse((self.root / "objects").exists())

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(dispatch, "maybe_host_self_update"),
            mock.patch.object(dispatch, "ensure_user_mcp_catalog"),
            mock.patch.object(
                dispatch.PerformanceRecorder,
                "span",
                side_effect=AssertionError("telemetry cache write"),
            ),
        ):
            status = dispatch.main(
                ["--json", "render", "html", "--project", str(self.root), *arguments],
                stdout=stdout,
                stderr=stderr,
            )
        return status, json.loads(stdout.getvalue())

    def test_public_cli_envelope_schema_success_and_repeat(self):
        status, envelope = self.cli()
        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["schema"], "literate-ai/cli-result@1")
        self.assertEqual(envelope["command"], "render.html")
        contracts = schema_tests.HtmlObservabilitySchemaTests()
        contracts.setUp()
        contracts.validator(HtmlRenderResult.SCHEMA).validate(envelope["result"])
        first = self.output.read_bytes()
        status, envelope = self.cli()
        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["result"]["status"], "cached")
        self.assertEqual(first, self.output.read_bytes())

    def test_public_cli_refusals_are_nonzero_typed_results(self):
        for arguments, code in (
            (("--surface", "unknown"), "render.unsupported_surface"),
            (("--view", "unknown"), "render.unknown_view"),
            (("--output", "../outside.html"), "render.output_outside_project"),
            (
                ("--output", str(self.root / "absolute.html")),
                "render.output_outside_project",
            ),
        ):
            with self.subTest(arguments=arguments):
                status, envelope = self.cli(*arguments)
                self.assertNotEqual(status, 0)
                self.assert_refused(
                    HtmlRenderResult.from_dict(envelope["result"]), code
                )
        self.assertFalse(self.output.exists())
