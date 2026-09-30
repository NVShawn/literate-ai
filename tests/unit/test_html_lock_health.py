"""Lock-health HTML follows live artifact checks and preserves diagnostics."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jsonschema import Draft202012Validator

from literate_ai.adapters import html_staleness, html_surfaces
from literate_ai.adapters.html_emitter import HtmlEmission, emit_html
from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.adapters.project_lock_health import observe_project_locks
from literate_ai.cli.component_locks import component_lock_from_args
from literate_ai.cli.verify import _locks
from literate_ai.contracts.html_observability import (
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlView,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.schema_catalog import schema_path
from tests.unit.test_component_lock_planning import _fixture
from tests.unit.test_html_emitter import DISTRIBUTION, STAMP
from tests.unit.test_html_render import _project
from tests.unit.test_repository_orchestration import snapshot


class LockHealthHtmlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        _project(self.root)
        self.request = HtmlRenderRequest(
            "lock-health",
            HtmlView("health", "1.0.0", "project", "literate-ai"),
            "locks.html",
            "off",
            "inline-only",
        )

    def emit(self):
        return emit_html(
            self.root,
            self.request,
            framework_distribution_identity=DISTRIBUTION,
            schema_catalog_release="1.1.0",
            generated_at=STAMP,
        )

    def test_live_audit_drift_changes_report_and_stales_retained_html(self):
        component, _ = _fixture(self.root)
        path = self.root / "literate.project.json"
        definition = json.loads(path.read_text())
        definition.update(
            component_roots=["greeting"],
            flavor_roots=["flavors"],
            component_flavor_selectors={"greeting": ["+macos", "+python"]},
        )
        path.write_text(json.dumps(definition))
        report, status = component_lock_from_args(
            SimpleNamespace(
                component=str(component),
                target="host",
                flavor=[],
                flavor_root=[],
                check=False,
                diff=False,
            )
        )
        self.assertEqual(status, 0, report)
        before = snapshot(self.root)
        observation = observe_project_locks(self.root)
        emitted = self.emit()
        self.assertIsInstance(emitted, HtmlEmission, emitted)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(observation.gate.state, "pass")
        self.assertEqual(_locks(self.root).state, observation.gate.state)
        self.assertEqual(
            observation.components[0]["report"]["catalog_audit"]["state"], "current"
        )
        self.assertIn(b"greeting", emitted.content)
        self.assertIn(b"Resolution audit: <strong>current</strong>", emitted.content)
        self.assertEqual(emitted.artifact.provenance.external_assets, ())
        self.assertEqual(
            emitted.artifact.provenance.source_bindings[0].source_identity,
            canonical_identity(observation.to_dict()),
        )
        output = self.root / self.request.output_path
        output.write_bytes(emitted.content)
        with mock.patch.object(
            html_staleness,
            "observe_html_framework",
            return_value=HtmlFrameworkObservation(DISTRIBUTION, "1.1.0"),
        ):
            self.assertEqual(
                html_staleness.verify_html_artifact(self.root, self.request).status,
                "current",
            )
            output.write_bytes(
                emitted.content.replace(b"Lock verification", b"All fine")
            )
            self.assertEqual(
                html_staleness.verify_html_artifact(self.root, self.request).status,
                "unpinned",
            )
            output.write_bytes(emitted.content)
            (component / "component.resolution-audit.host.json").unlink()
            observed = observe_project_locks(self.root)
            self.assertEqual(observed.gate.state, "fail")
            self.assertEqual(observed.components[0]["state"], "not-current")
            self.assertEqual(
                observed.components[0]["report"]["catalog_audit"]["state"], "missing"
            )
            currency = html_staleness.verify_html_artifact(self.root, self.request)
            self.assertEqual(currency.status, "stale")
            self.assertEqual(currency.stale_source_labels, ("lock-health",))

    def test_skipped_scope_is_visible_without_calling_full_verification(self):
        with mock.patch(
            "literate_ai.cli.verify.verify_project_from_args",
            side_effect=AssertionError("recursive verification"),
        ):
            emitted = self.emit()
        self.assertIsInstance(emitted, HtmlEmission)
        self.assertIn(b"<strong>skipped</strong>", emitted.content)
        self.assertIn(b"No Component checks were performed.", emitted.content)
        self.assertNotIn(b"<script src=", emitted.content)

    def test_refusals_are_escaped_and_malformed_reports_cannot_render(self):
        report = observe_project_locks(self.root).to_dict()
        report["gate"].update(state="fail", detail="fixture refusal")
        message = '</script><script>alert("x")</script>'
        report["components"] = [
            {
                "component": "components/one",
                "state": "error",
                "report": None,
                "error": {"code": "component_lock.inputs_changed", "message": message},
            }
        ]
        with mock.patch.object(
            html_surfaces,
            "observe_project_locks",
            return_value=SimpleNamespace(to_dict=lambda: report),
        ):
            emitted = self.emit()
        self.assertIsInstance(emitted, HtmlEmission)
        self.assertIn(b"component_lock.inputs_changed", emitted.content)
        self.assertNotIn(message.encode(), emitted.content)
        malformed = copy.deepcopy(report)
        malformed["components"][0]["state"] = "current"
        for invalid in (malformed, {**report, "project_id": "elsewhere"}):
            with mock.patch.object(
                html_surfaces,
                "observe_project_locks",
                return_value=SimpleNamespace(to_dict=lambda invalid=invalid: invalid),
            ):
                self.assertIsInstance(self.emit(), HtmlRenderRefusal)
        self.request = replace(
            self.request, view=replace(self.request.view, view_version="2.0.0")
        )
        self.assertEqual(self.emit().code, "render.unknown_view")

    def test_open_command_diagnostics_preserve_exact_row_currentness(self):
        validator = Draft202012Validator(
            json.loads(
                schema_path(
                    "project-lock-health.schema.json", catalog_version="v2"
                ).read_text(encoding="utf-8")
            )
        )
        report = observe_project_locks(self.root).to_dict()
        row = {
            "component": "components/one",
            "state": "current",
            "report": {
                "schema": "literate-ai/component-lock-command@1",
                "current": True,
                "producer_diagnostics": {"nested": ["retained verbatim"]},
            },
            "error": None,
        }
        report["components"] = [row]
        self.assertTrue(validator.is_valid(report))
        row["report"]["current"] = False
        self.assertFalse(validator.is_valid(report))
        row["state"] = "not-current"
        self.assertTrue(validator.is_valid(report))
        row["report"]["current"] = True
        self.assertFalse(validator.is_valid(report))
        row["report"] = None
        self.assertFalse(validator.is_valid(report))
        row["state"] = "error"
        row["error"] = {"code": "unavailable", "message": "fixture"}
        self.assertTrue(validator.is_valid(report))
        row["unreviewed"] = True
        self.assertFalse(validator.is_valid(report))
