"""Public verify verdicts over real fixture files; only installation is synthetic."""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_render, html_staleness
from literate_ai.adapters.html_emitter import HtmlEmission, emit_html
from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.cli import dispatch
from literate_ai.contracts.html_observability import (
    HtmlProvenance,
    HtmlRenderRefusal,
    HtmlStalenessReport,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import discover_project
from literate_ai.schema_catalog import verify_schema_catalog
from tests.unit import test_html_observability_schema as schema_tests
from tests.unit.test_html_emitter import DISTRIBUTION, STAMP, request
from tests.unit.test_html_render import _project


class HtmlStalenessTests(unittest.TestCase):
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

    def replace_block(self, name, change):
        content = self.output.read_text(encoding="utf-8")
        pattern = rf'(<script\b[^>]*\bid="{name}"[^>]*>)(.*?)(</script>)'
        match = re.search(pattern, content, re.DOTALL)
        self.assertIsNotNone(match)
        self.output.write_text(
            content[: match.start(2)]
            + change(match.group(2))
            + content[match.end(2) :],
            encoding="utf-8",
            newline="\n",
        )

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

    def test_actual_writer_inventory_matches_exact_html_write_contracts(self):
        documents = [discover_project(self.root).definition.to_dict()]
        for options, expected_status in (([], 0), (["--surface", "unknown"], 1)):
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(dispatch, "maybe_host_self_update"),
                mock.patch.object(dispatch, "ensure_user_mcp_catalog"),
                mock.patch.object(
                    html_render, "observe_html_framework", return_value=self.framework
                ),
            ):
                status = dispatch.main(
                    [
                        "render",
                        "html",
                        "--project",
                        str(self.root),
                        "--cache-mode",
                        "off",
                        *options,
                    ],
                    stdout=out,
                    stderr=err,
                )
            document = json.loads(out.getvalue() or err.getvalue())
            self.assertEqual(status, expected_status, document)
            documents.append(document)
        status, gate = self.verify()
        self.assertEqual(status, 0, gate)
        documents.append(gate)
        prefix = "urn:literate-ai:schema:v1:html-observability-"

        def contracts(value):
            if isinstance(value, dict):
                schema = value.get("schema")
                if isinstance(schema, str) and schema.startswith(prefix):
                    yield schema
                for nested in value.values():
                    yield from contracts(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from contracts(nested)

        observed = set(contracts(documents))
        expected = {
            prefix + name
            for name in (
                "artifact",
                "external-asset",
                "provenance",
                "render-request",
                "render-refusal",
                "render-result",
                "renderer-binding",
                "source-binding",
                "staleness-report",
                "view",
            )
        }
        self.assertEqual(observed, expected)
        matrix = json.loads(
            (schema_tests.V2_ROOT / "compatibility.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {
                schema
                for schema in matrix["write_contracts"]
                if schema.startswith(prefix)
            },
            observed,
        )
        self.assertNotIn(prefix + "surface", observed)
        # Catalog verification independently requires the Python allowlist to match.
        verify_schema_catalog("v2", schema_tests.V2_ROOT)

    def test_verify_bypasses_implicit_host_and_operator_configuration_mutations(self):
        self.render()
        before = self.snapshot()
        with (
            mock.patch.object(
                dispatch,
                "maybe_host_self_update",
                side_effect=AssertionError("host update"),
            ),
            mock.patch.object(
                dispatch,
                "ensure_user_mcp_catalog",
                side_effect=AssertionError("user config"),
            ),
            mock.patch.object(
                dispatch.PerformanceRecorder,
                "span",
                side_effect=AssertionError("telemetry"),
            ),
        ):
            self.assert_report("current")
        self.assertEqual(self.snapshot(), before)

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

    def test_nested_invocation_uses_declaring_project_root(self):
        self.render()
        status, gate = self.verify(self.root / "skills/agent")
        self.assertEqual(status, 0, gate)
        self.assertEqual(gate["artifacts"][0]["status"], "current")

    def test_changed_declarations_invalidate_the_gate_observation(self):
        self.render()
        original = html_staleness.verify_html_artifact

        def changed(root, request):
            report = original(root, request)
            self.declare()
            return report

        with mock.patch.object(
            html_staleness, "verify_html_artifact", side_effect=changed
        ):
            status, gate = self.verify()
        self.assertEqual(status, 1, gate)
        self.assertEqual(gate["artifacts"], [])
        self.assertIn("declarations changed", gate["detail"])

    def test_changed_installed_distribution_is_named_as_renderer_drift(self):
        self.render()
        self.observer.return_value = replace(
            self.framework,
            framework_distribution_identity=canonical_identity(
                {"fixture": "new-wheel"}
            ),
        )
        record = self.assert_report("stale")
        self.assertEqual(record.stale_source_labels, ("renderer",))

    def test_old_view_provenance_is_stale_without_requiring_current_markup(self):
        original = self.render().artifact.provenance
        changed = HtmlProvenance.create(
            view=replace(original.view, view_version="0.9.0"),
            source_bindings=original.source_bindings,
            renderer=original.renderer,
            external_assets=original.external_assets,
            generated_at=original.generated_at,
        )
        self.replace_block("litai-provenance", lambda _: json.dumps(changed.to_dict()))
        record = self.assert_report("stale")
        self.assertEqual(record.stale_source_labels, ("view",))

    def test_missing_artifact_including_missing_parent_is_a_finding(self):
        self.assert_report("missing")
        self.declare(replace(self.request, output_path="absent/graph.html"))
        self.assert_report("missing")
        self.assertFalse((self.root / "absent").exists())

    def test_unreadable_regular_file_is_a_finding(self):
        self.render()
        with mock.patch.object(
            html_staleness, "read_output", side_effect=PermissionError
        ):
            self.assert_report("unreadable")

    def test_directory_artifact_is_unreadable(self):
        self.output.mkdir()
        self.assert_report("unreadable")

    def test_indirect_artifact_is_unreadable_without_following(self):
        target = self.root / "private.html"
        target.write_bytes(b"unrelated content")
        try:
            self.output.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"host cannot create symbolic links: {exc}")
        self.assert_report("unreadable")
        self.assertEqual(target.read_bytes(), b"unrelated content")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_fifo_is_unreadable_without_blocking(self):
        os.mkfifo(self.output)
        self.assert_report("unreadable")

    def test_absent_malformed_and_ambiguous_provenance_are_unpinned(self):
        emitted = self.render()
        for content in (b"", b"<html>handwritten</html>", b"\xff"):
            with self.subTest(content=content):
                self.output.write_bytes(content)
                self.assert_report("unpinned")
        for change in (
            lambda _: "{not json}",
            lambda text: '{"schema":"duplicate",' + text.lstrip()[1:],
            lambda _: json.dumps({"schema": "wrong"}),
        ):
            self.output.write_bytes(emitted.content)
            self.replace_block("litai-provenance", change)
            self.assert_report("unpinned")
        for change in (
            lambda text: (
                text
                + '<script id="litai-provenance" type="application/json">{}</script>'
            ),
            lambda text: text.replace(
                'id="litai-provenance"', 'id="litai-provenance" src="other.json"'
            ),
            lambda text: text.replace(
                'id="litai-provenance"', 'id="litai-provenance" id="duplicate"'
            ),
        ):
            self.output.write_text(
                change(emitted.content.decode("utf-8")), encoding="utf-8", newline="\n"
            )
            self.assert_report("unpinned")

    def test_provenance_fixture_is_utf8_under_a_legacy_code_page(self):
        original = Path.open

        def legacy_open(
            path, mode="r", buffering=-1, encoding=None, errors=None, newline=None
        ):
            if "b" not in mode and encoding in {None, "locale"}:
                encoding = "cp1252"
            return original(
                path,
                mode,
                buffering=buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )

        with mock.patch.object(Path, "open", new=legacy_open):
            self.test_absent_malformed_and_ambiguous_provenance_are_unpinned()

    def test_borrowed_current_provenance_cannot_cover_edited_visible_content(self):
        emitted = self.render()
        self.output.write_bytes(
            emitted.content.replace(b"<main>", b"<main><p>Unbound addition</p>", 1)
        )
        self.assert_report("unpinned")

    def test_borrowed_current_provenance_cannot_cover_forged_source_or_excerpts(self):
        emitted = self.render()
        for block in ("litai-source", "litai-excerpts"):
            with self.subTest(block=block):
                self.output.write_bytes(emitted.content)
                self.replace_block(block, lambda _: "{}")
                self.assert_report("unpinned")

    def test_no_declarations_skip_without_installation_or_cache_observation(self):
        self.declare()
        self.output.write_bytes(b"unrelated application page")
        before = self.snapshot()
        self.observer.side_effect = AssertionError("installation must not be observed")
        with mock.patch.object(
            dispatch.PerformanceRecorder, "span", side_effect=AssertionError("write")
        ):
            status, gate = self.verify()
        self.assertEqual(status, 0)
        self.assertEqual(gate["state"], "skipped")
        self.assertEqual(gate["artifacts"], [])
        self.assertEqual(self.snapshot(), before)

    def test_unavailable_installation_does_not_fabricate_expected_identity(self):
        self.observer.return_value = HtmlRenderRefusal(
            "render.surface_unavailable", "Synthetic unavailable installation."
        )
        self.assert_refused()

    def test_unsupported_declarations_refuse_without_fabricated_reports(self):
        for changed, code in (
            (replace(self.request, surface_id="unknown"), "render.unsupported_surface"),
            (
                replace(
                    self.request, view=replace(self.request.view, view_id="unknown")
                ),
                "render.unknown_view",
            ),
            (
                replace(self.request, external_asset_policy="inline-only"),
                "render.external_asset_forbidden",
            ),
        ):
            with self.subTest(code=code):
                self.declare(changed)
                self.assert_refused(code)

    def test_each_cache_mode_is_ignored_without_creating_cache_or_lock_files(self):
        for mode in ("off", "read-only", "write-only", "read-write"):
            with self.subTest(mode=mode):
                self.declare(replace(self.request, cache_mode=mode))
                self.render()
                before = self.snapshot()
                self.assert_report("current")
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.root / "objects").exists())
                self.assertFalse((self.root / ".litai-locks").exists())

    def test_multiple_declarations_keep_independent_reports_and_failure_exit(self):
        self.declare(self.request, replace(self.request, output_path="missing.html"))
        self.render()
        status, gate = self.verify()
        self.assertEqual(status, 1)
        self.assertEqual(
            [item["status"] for item in gate["artifacts"]], ["current", "missing"]
        )

    def test_installation_drift_during_verification_cannot_return_current(self):
        self.render()
        self.observer.side_effect = [
            self.framework,
            replace(self.framework, schema_catalog_release="1.2.0"),
        ]
        self.assert_refused()

    def test_source_drift_during_verification_cannot_return_current(self):
        self.render()
        original = html_staleness.read_output

        def changed(target):
            snapshot = original(target)
            skill = self.root / "skills/agent/SKILL.md"
            skill.write_text(
                skill.read_text(encoding="utf-8") + "\nChanged during observation.\n",
                encoding="utf-8",
                newline="\n",
            )
            return snapshot

        with mock.patch.object(html_staleness, "read_output", side_effect=changed):
            self.assert_refused()

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
