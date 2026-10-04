"""Version-health output preserves producer decisions and exact currency checks."""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_staleness, html_surfaces
from literate_ai.adapters.html_emitter import HtmlEmission, emit_html
from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.contracts.html_observability import (
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlView,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.version_check import check_versions
from tests.support.fixtures_test_html_emitter import DISTRIBUTION, ROOT, STAMP
from tests.support.fixtures_test_html_render import _project


class VersionHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = check_versions(project_path=ROOT, require_project=True)

    def setUp(self):
        self.request = HtmlRenderRequest(
            "version-check",
            HtmlView("health", "1.0.0", "project", "literate-ai"),
            "version.html",
            "off",
            "inline-only",
        )

    def emit(self, root=ROOT):
        return emit_html(
            root,
            self.request,
            framework_distribution_identity=DISTRIBUTION,
            schema_catalog_release="1.1.0",
            generated_at=STAMP,
        )

    def test_real_report_is_bound_without_invoking_project_verification(self):
        with mock.patch(
            "literate_ai.cli.verify.verify_project_from_args",
            side_effect=AssertionError("recursive verification"),
        ):
            source = html_surfaces.load_html_surface(ROOT, self.request)
        self.assertIsInstance(source, html_surfaces.HtmlSurfaceSource)
        self.assertEqual(source.document(), self.report)
        self.assertEqual(
            source.binding.source_identity, canonical_identity(self.report)
        )
        emitted = self.emit()
        self.assertIsInstance(emitted, HtmlEmission)
        self.assertEqual(emitted.artifact.provenance.external_assets, ())
        self.assertEqual(
            emitted.artifact.provenance.renderer.renderer_id, "version-check-html"
        )
        self.assertNotIn(b"<script src=", emitted.content)
        self.assertIn(b"Version agreement", emitted.content)

    def test_failed_and_warning_reports_are_preserved_and_escaped(self):
        for ok in (False, True):
            report = copy.deepcopy(self.report)
            report["ok"] = ok
            report["distribution"]["ok"] = ok
            report["distribution"]["diagnostic"] = {
                "code": "fixture.warning",
                "message": '</script><script>alert("x")</script>',
            }
            with (
                self.subTest(ok=ok),
                mock.patch.object(html_surfaces, "check_versions", return_value=report),
            ):
                emitted = self.emit()
            self.assertIsInstance(emitted, HtmlEmission)
            self.assertIn(
                (
                    "<strong>"
                    + ("Pass" if ok else "Fail")
                    + "</strong>Version agreement"
                ).encode(),
                emitted.content,
            )
            self.assertIn(b"fixture.warning", emitted.content)
            self.assertNotIn(b'<script>alert("x")</script>', emitted.content)
            self.assertEqual(
                emitted.artifact.provenance.source_bindings[0].source_identity,
                canonical_identity(report),
            )

    def test_malformed_or_wrong_project_reports_cannot_render(self):
        for change in (
            {"ok": "yes"},
            {"project": {**self.report["project"], "project_id": "elsewhere"}},
        ):
            with (
                self.subTest(change=change),
                mock.patch.object(
                    html_surfaces,
                    "check_versions",
                    return_value={**self.report, **change},
                ),
            ):
                self.assertIsInstance(self.emit(), HtmlRenderRefusal)
        self.request = replace(
            self.request, view=replace(self.request.view, view_version="2.0.0")
        )
        self.assertEqual(self.emit().code, "render.unknown_view")

    def test_report_drift_and_visible_tampering_fail_currency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _project(root)
            framework = HtmlFrameworkObservation(DISTRIBUTION, "1.1.0")
            with (
                mock.patch.object(
                    html_surfaces, "check_versions", return_value=self.report
                ),
                mock.patch.object(
                    html_staleness, "observe_html_framework", return_value=framework
                ),
            ):
                emitted = self.emit(root)
                self.assertIsInstance(emitted, HtmlEmission)
                output = root / self.request.output_path
                output.write_bytes(emitted.content)
                current = html_staleness.verify_html_artifact(root, self.request)
                self.assertEqual(current.status, "current")
                output.write_bytes(
                    emitted.content.replace(b"Version agreement", b"Everything is fine")
                )
                self.assertEqual(
                    html_staleness.verify_html_artifact(root, self.request).status,
                    "unpinned",
                )
                output.write_bytes(emitted.content)
            changed = copy.deepcopy(self.report)
            changed["tag"]["head"] = "a" * 40
            with (
                mock.patch.object(
                    html_surfaces, "check_versions", return_value=changed
                ),
                mock.patch.object(
                    html_staleness, "observe_html_framework", return_value=framework
                ),
            ):
                stale = html_staleness.verify_html_artifact(root, self.request)
                self.assertEqual(stale.status, "stale")
                self.assertEqual(stale.stale_source_labels, ("version-check",))


if __name__ == "__main__":
    unittest.main()
