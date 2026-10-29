"""Public verify verdicts over real fixture files; only installation is synthetic."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_emitter, html_staleness
from literate_ai.adapters.html_emitter import HtmlEmission, emit_html
from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.cli import dispatch
from literate_ai.contracts.html_observability import (
    HtmlRenderRequest,
    HtmlStalenessReport,
    HtmlView,
)
from literate_ai.contracts.identity import canonical_identity
from tests.support import fixtures_test_html_observability_schema as schema_tests
from tests.support.fixtures_test_html_emitter import DISTRIBUTION, STAMP, request
from tests.support.fixtures_test_html_render import _project


class HtmlStalenessTests(unittest.TestCase):
    def test_version_check_remains_current_after_render_and_commit(self):
        self.request = HtmlRenderRequest(
            "version-check",
            HtmlView("health", "1.0.0", "project", "literate-ai"),
            "version.html",
            "off",
            "inline-only",
        )
        self.declare(self.request)
        self.output = self.root / "version.html"
        manifest = self.root / "literate.project.json"
        definition = json.loads(manifest.read_text())
        definition.pop("lifecycle_driver", None)
        definition.pop("test_receipt_policy", None)
        manifest.write_text(json.dumps(definition))

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )

        git("init", "--quiet")
        git("add", ".")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Initial authority",
        )
        with mock.patch(
            "literate_ai.version_check.cli_version_label",
            return_value="1.3.0 (development source; dirty)",
        ):
            self.render()
            self.assert_report("current")
        git("add", "version.html")
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Retain version view",
        )
        with mock.patch(
            "literate_ai.version_check.cli_version_label",
            return_value="1.3.0 (development source)",
        ):
            self.assert_report("current")
            major, minor, patch = map(int, str(definition["version"]).split("."))
            definition["version"] = f"{major}.{minor}.{patch + 1}"
            manifest.write_text(json.dumps(definition))
            self.assert_report("stale")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        _project(self.root)
        self.request = request("literate-ai", policy="pinned-cdn")
        self.declare(self.request)
        self.output = self.root / self.request.output_path
        self.framework = HtmlFrameworkObservation(DISTRIBUTION, "1.1.0")
        observation = mock.patch.object(
            html_staleness, "observe_html_framework", return_value=self.framework
        )
        self.observer = observation.start()
        self.addCleanup(observation.stop)
        environment = mock.patch.dict(
            os.environ,
            {
                "OBJ_DIR": str(self.root / "objects"),
                "BUILD_DIR": str(self.root / "build"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def declare(self, *requests):
        manifest = self.root / "literate.project.json"
        definition = json.loads(manifest.read_text(encoding="utf-8"))
        definition["html_render_requests"] = [item.to_dict() for item in requests]
        manifest.write_text(json.dumps(definition), encoding="utf-8", newline="\n")

    def render(self):
        emitted = emit_html(
            self.root,
            self.request,
            framework_distribution_identity=self.framework.framework_distribution_identity,
            schema_catalog_release=self.framework.schema_catalog_release,
            generated_at=STAMP,
        )
        self.assertIsInstance(emitted, HtmlEmission)
        self.output.write_bytes(emitted.content)
        return emitted

    def verify(self, path=None):
        out, err = io.StringIO(), io.StringIO()
        status = dispatch.main(
            ["verify", str(path or self.root), "--gate", "html-observability"],
            stdout=out,
            stderr=err,
        )
        envelope = json.loads(out.getvalue() or err.getvalue())
        self.assertTrue(envelope["ok"], envelope)
        gates = envelope["result"]["gates"]
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["gate"], "html-observability")
        return status, gates[0]

    def assert_report(self, expected):
        status, gate = self.verify()
        self.assertEqual(status, 0 if expected == "current" else 1, gate)
        self.assertEqual(gate["state"], "pass" if expected == "current" else "fail")
        self.assertEqual(len(gate["artifacts"]), 1, gate)
        record = HtmlStalenessReport.from_dict(gate["artifacts"][0])
        self.assertEqual(record.status, expected)
        return record

    def assert_refused(self, code="render.surface_unavailable"):
        status, gate = self.verify()
        self.assertEqual(status, 1, gate)
        self.assertEqual(gate["state"], "fail")
        self.assertEqual(gate["artifacts"], [])
        self.assertIn(code, gate["detail"])

    def snapshot(self):
        return {
            path.relative_to(self.root): (
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in self.root.rglob("*")
        }

    def test_current_artifact_matches_contract_and_preserves_all_files(self):
        emitted = self.render()
        before = self.snapshot()
        with mock.patch.object(
            dispatch.PerformanceRecorder, "span", side_effect=AssertionError("write")
        ):
            record = self.assert_report("current")
            self.assertEqual(self.assert_report("current"), record)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            record.expected_render_inputs_identity,
            emitted.artifact.provenance.render_inputs_identity,
        )
        schemas = schema_tests.HtmlObservabilitySchemaTests()
        schemas.setUp()
        schemas.validator(record.SCHEMA).validate(record.to_dict())

    def test_actual_changed_source_is_named_in_stale_report(self):
        emitted = self.render()
        skill = self.root / "skills/agent/SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "\nChanged project instruction.\n",
            encoding="utf-8",
            newline="\n",
        )
        before = self.snapshot()
        record = self.assert_report("stale")
        self.assertEqual(record.stale_source_labels, ("authority-graph",))
        self.assertEqual(
            record.observed_render_inputs_identity,
            emitted.artifact.provenance.render_inputs_identity,
        )
        self.assertEqual(self.snapshot(), before)

    def test_artifact_drift_at_final_observation_is_preserved_and_refuses(self):
        self.render()
        original = html_staleness.read_output
        calls = 0

        def changed(target):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_bytes(b"concurrent replacement")
            return original(target)

        with mock.patch.object(html_staleness, "read_output", side_effect=changed):
            self.assert_refused()
        self.assertEqual(self.output.read_bytes(), b"concurrent replacement")

    def test_currency_keeps_two_complete_input_observations(self):
        self.render()
        before = self.snapshot()
        with mock.patch.object(
            html_emitter, "_load_html_inputs", wraps=html_emitter._load_html_inputs
        ) as observe:
            self.assert_report("current")
        self.assertEqual(observe.call_count, 2)
        self.assertEqual(self.observer.call_count, 2)
        self.assertEqual(self.snapshot(), before)

    def test_source_drift_after_reconstruction_is_preserved_and_refuses(self):
        self.render()
        original = html_staleness._emit_inputs
        skill = self.root / "skills/agent/SKILL.md"
        changed = skill.read_bytes() + b"\nConcurrent authored change.\n"

        def reconstruct(*args, **kwargs):
            emission = original(*args, **kwargs)
            skill.write_bytes(changed)
            return emission

        with mock.patch.object(html_staleness, "_emit_inputs", side_effect=reconstruct):
            self.assert_refused()
        self.assertEqual(skill.read_bytes(), changed)

    def test_framework_drift_at_final_observation_still_refuses(self):
        self.render()
        before = self.snapshot()
        self.observer.side_effect = [
            self.framework,
            HtmlFrameworkObservation(
                canonical_identity({"substituted": True}), "1.1.0"
            ),
        ]
        self.assert_refused()
        self.assertEqual(self.snapshot(), before)
