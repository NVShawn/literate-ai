"""Phase 2 performance and workflow/routing observability surfaces."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from literate_ai.adapters import html_emitter, html_surfaces
from literate_ai.contracts.html_observability import HtmlRenderRequest, HtmlView
from literate_ai.contracts.identity import canonical_identity
from literate_ai.perf import PerformanceSpan

ROOT = Path(__file__).resolve().parents[2]
STAMP = "2026-09-16T00:00:00Z"
DISTRIBUTION = canonical_identity({"test_fixture": "html-phase2"})


def request(surface: str, view: str, output: str) -> HtmlRenderRequest:
    return HtmlRenderRequest(
        surface,
        HtmlView(view, "1.0.0", "project", "literate-ai"),
        output,
        "off",
        "inline-only",
    )


class HtmlPhase2SurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v2/html-observability-sources.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)

    def test_workflow_routing_uses_declared_catalogs_and_exact_bytes(self) -> None:
        selected = request("workflow-routing", "catalog", "workflow-routing.html")
        source = html_surfaces.load_html_surface(ROOT, selected)
        self.assertIsInstance(source, html_surfaces.HtmlSurfaceSource)
        document = source.document()
        self.assertEqual(document["project_id"], "literate-ai")
        self.assertTrue(document["workflows"])
        self.assertTrue(document["routes"])
        self.assertTrue(
            all(
                item["content_identity"].startswith("sha256:")
                for item in document["workflows"]
            )
        )
        emitted = html_emitter._emit_html(
            ROOT,
            selected,
            framework_distribution_identity=DISTRIBUTION,
            schema_catalog_release="0.1.1",
            generated_at=STAMP,
        )
        self.assertIsInstance(emitted, html_emitter.HtmlEmission)
        self.assertIn(b"Workflow and routing", emitted.content)
        self.assertIn(b"Stage dependencies expose execution nesting", emitted.content)

    def test_performance_history_preserves_failures_and_empty_state(self) -> None:
        selected = request("performance-history", "history", "performance.html")
        spans = [
            PerformanceSpan(
                run_id="run-1",
                stage="test",
                target_kind="project",
                target_id="literate-ai",
                coding_cli=None,
                model=None,
                started_at="2026-09-16T00:00:00+00:00",
                ended_at="2026-09-16T00:00:01+00:00",
                duration_ms=1000,
                ok=False,
                error_code="test.failed",
                extra={},
            )
        ]
        with mock.patch.object(
            html_surfaces, "read_performance_spans", return_value=spans
        ):
            emitted = html_emitter._emit_html(
                ROOT,
                selected,
                framework_distribution_identity=DISTRIBUTION,
                schema_catalog_release="0.1.1",
                generated_at=STAMP,
            )
        self.assertIsInstance(emitted, html_emitter.HtmlEmission)
        self.assertIn(b"Performance history", emitted.content)
        self.assertIn(b"test.failed", emitted.content)
        self.assertIn(b"Failures", emitted.content)

        with mock.patch.object(
            html_surfaces, "read_performance_spans", return_value=[]
        ):
            empty = html_emitter._emit_html(
                ROOT,
                selected,
                framework_distribution_identity=DISTRIBUTION,
                schema_catalog_release="0.1.1",
                generated_at=STAMP,
            )
        self.assertIn(b"No performance spans are available", empty.content)

    def test_new_surface_views_fail_closed(self) -> None:
        for surface, view in (
            ("performance-history", "catalog"),
            ("workflow-routing", "history"),
        ):
            refused = html_surfaces.load_html_surface(
                ROOT, request(surface, view, "refused.html")
            )
            self.assertEqual(refused.code, "render.unknown_view")


if __name__ == "__main__":
    unittest.main()
