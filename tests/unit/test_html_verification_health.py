"""Verification views preserve selected-gate evidence without recursive checks."""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_staleness, html_surfaces, project_verification
from literate_ai.adapters.html_emitter import HtmlEmission, emit_html
from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.contracts.html_observability import (
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlView,
)
from literate_ai.contracts.identity import canonical_identity
from tests.support.fixtures_test_html_emitter import DISTRIBUTION, STAMP
from tests.support.fixtures_test_html_render import _project


class VerificationHealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        _project(self.root)
        self.request = HtmlRenderRequest(
            "verification-health",
            HtmlView("health", "1.0.0", "project", "literate-ai"),
            "verification.html",
            "off",
            "inline-only",
        )
        self.report = {
            "schema": "literate-ai/project-verify@1",
            "project": str(self.root),
            "gates": [
                {"gate": gate, "state": "skipped", "detail": "No policy declared."}
                for gate in html_surfaces.VERIFICATION_HEALTH_GATES
            ],
            "counts": {"pass": 0, "fail": 0, "skipped": 4},
            "ok": True,
        }

    def emit(self):
        return emit_html(
            self.root,
            self.request,
            framework_distribution_identity=DISTRIBUTION,
            schema_catalog_release="1.1.0",
            generated_at=STAMP,
        )

    def test_real_source_uses_shared_gates_without_cli_or_recursive_html(self):
        with (
            mock.patch(
                "literate_ai.cli.verify.verify_project_from_args",
                side_effect=AssertionError("CLI called"),
            ),
            mock.patch.dict(
                project_verification._GATES,
                {
                    "html-observability": mock.Mock(
                        side_effect=AssertionError("recursive HTML")
                    )
                },
            ),
        ):
            source = html_surfaces.load_html_surface(self.root, self.request)
            report, _ = project_verification.observe_project_verification(
                self.root, gates=html_surfaces.VERIFICATION_HEALTH_GATES
            )
        self.assertIsInstance(source, html_surfaces.HtmlSurfaceSource)
        self.assertEqual(source.document(), report)
        self.assertEqual(source.binding.source_identity, canonical_identity(report))

    def test_failure_skips_and_escaping_are_visible(self):
        self.report["gates"][0].update(
            state="fail", detail='<script>alert("x")</script>'
        )
        self.report.update(counts={"pass": 0, "fail": 1, "skipped": 3}, ok=False)
        with mock.patch.object(
            html_surfaces, "observe_project_verification", return_value=(self.report, 1)
        ) as observe:
            emitted = self.emit()
        observe.assert_called_with(
            self.root, gates=html_surfaces.VERIFICATION_HEALTH_GATES
        )
        self.assertIsInstance(emitted, HtmlEmission)
        self.assertIn(b"Selected gates: Fail", emitted.content)
        self.assertIn(b"HTML-artifact gate is excluded", emitted.content)
        self.assertIn(b"</strong>Skipped", emitted.content)
        self.assertNotIn(b'<script>alert("x")</script>', emitted.content)
        self.assertIn(b"&lt;script&gt;", emitted.content)
        self.assertEqual(emitted.artifact.provenance.external_assets, ())
        self.assertEqual(
            emitted.artifact.provenance.renderer.renderer_id, "verification-health-html"
        )

    def test_invalid_reports_scope_and_unavailable_source_refuse(self):
        for change in (
            {"ok": "yes"},
            {"ok": False},
            {"project": "/elsewhere"},
            {"gates": []},
            {"counts": {"pass": 4, "fail": 0, "skipped": 0}},
        ):
            with (
                self.subTest(change=change),
                mock.patch.object(
                    html_surfaces,
                    "observe_project_verification",
                    return_value=({**self.report, **change}, 0),
                ),
            ):
                self.assertIsInstance(self.emit(), HtmlRenderRefusal)
        with mock.patch.object(
            html_surfaces,
            "observe_project_verification",
            side_effect=OSError("unavailable"),
        ):
            self.assertEqual(self.emit().code, "render.surface_unavailable")
        for change in ({"scope_identifier": "elsewhere"}, {"view_version": "2.0.0"}):
            self.request = replace(
                self.request, view=replace(self.request.view, **change)
            )
            self.assertEqual(self.emit().code, "render.unknown_view")

    def test_report_drift_and_visible_tampering_fail_currency(self):
        framework = HtmlFrameworkObservation(DISTRIBUTION, "1.1.0")
        with (
            mock.patch.object(
                html_surfaces,
                "observe_project_verification",
                return_value=(self.report, 0),
            ),
            mock.patch.object(
                html_staleness, "observe_html_framework", return_value=framework
            ),
        ):
            emitted = self.emit()
            self.assertIsInstance(emitted, HtmlEmission)
            output = self.root / self.request.output_path
            output.write_bytes(emitted.content)
            self.assertEqual(
                html_staleness.verify_html_artifact(self.root, self.request).status,
                "current",
            )
            output.write_bytes(
                emitted.content.replace(b"Selected gates: Pass", b"Everything passed")
            )
            self.assertEqual(
                html_staleness.verify_html_artifact(self.root, self.request).status,
                "unpinned",
            )
            output.write_bytes(emitted.content)
        changed = copy.deepcopy(self.report)
        changed["gates"][0]["detail"] = "A different diagnostic."
        with (
            mock.patch.object(
                html_surfaces, "observe_project_verification", return_value=(changed, 0)
            ),
            mock.patch.object(
                html_staleness, "observe_html_framework", return_value=framework
            ),
        ):
            stale = html_staleness.verify_html_artifact(self.root, self.request)
            self.assertEqual(stale.status, "stale")
            self.assertEqual(stale.stale_source_labels, ("verification-health",))


if __name__ == "__main__":
    unittest.main()
