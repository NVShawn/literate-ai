"""Offline exact-artifact dashboard composition."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_dashboard


class HtmlDashboardTests(unittest.TestCase):
    def inputs(self) -> tuple[html_dashboard.DashboardInput, ...]:
        return (
            html_dashboard.DashboardInput(
                "one/health.html",
                "one",
                "verification-health",
                "health",
                "sha256:" + "1" * 64,
            ),
            html_dashboard.DashboardInput(
                "two/perf.html",
                "two",
                "performance-history",
                "history",
                "sha256:" + "2" * 64,
            ),
        )

    def test_two_projects_render_as_exact_offline_panes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = (root / "one", root / "two")
            with mock.patch.object(
                html_dashboard, "_observe", return_value=self.inputs()
            ) as observe:
                result = html_dashboard.render_dashboard(
                    root, projects, "observability/dashboard.html"
                )
            output = root / result["output"]
            content = output.read_bytes()
            self.assertEqual(result["project_count"], 2)
            self.assertEqual(result["artifact_count"], 2)
            self.assertEqual(
                html_dashboard._recognized(content)["artifacts"][1]["project_id"], "two"
            )
            self.assertIn(b'<iframe src="../one/health.html"', content)
            self.assertIn(b'<iframe src="../two/perf.html"', content)
            self.assertEqual(observe.call_count, 2)

    def test_repeat_is_byte_stable_and_foreign_output_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = (root / "one", root / "two")
            with mock.patch.object(
                html_dashboard, "_observe", return_value=self.inputs()
            ):
                first = html_dashboard.render_dashboard(
                    root, projects, "dashboard.html"
                )
                content = (root / "dashboard.html").read_bytes()
                second = html_dashboard.render_dashboard(
                    root, projects, "dashboard.html"
                )
            self.assertEqual(first, second)
            self.assertEqual((root / "dashboard.html").read_bytes(), content)

            (root / "dashboard.html").write_bytes(b"user owned")
            with (
                mock.patch.object(
                    html_dashboard, "_observe", return_value=self.inputs()
                ),
                self.assertRaises(ValueError),
            ):
                html_dashboard.render_dashboard(root, projects, "dashboard.html")
            self.assertEqual((root / "dashboard.html").read_bytes(), b"user owned")

    def test_input_identity_changes_dashboard_identity(self) -> None:
        first = html_dashboard._document(self.inputs())
        changed = list(self.inputs())
        changed[1] = html_dashboard.DashboardInput(
            changed[1].path,
            changed[1].project_id,
            changed[1].surface_id,
            changed[1].view_id,
            "sha256:" + "3" * 64,
        )
        second = html_dashboard._document(tuple(changed))
        self.assertNotEqual(first["dashboard_identity"], second["dashboard_identity"])


if __name__ == "__main__":
    unittest.main()
