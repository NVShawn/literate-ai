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

from literate_ai.adapters import html_publication, html_render
from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.cli import dispatch
from literate_ai.contracts.html_observability import HtmlArtifact, HtmlRenderResult
from literate_ai.storage import StorageError
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

    def assert_success(self, result, status="rendered"):
        self.assertEqual(result.status, status, result)
        self.assertIsNone(result.refusal)
        self.assertEqual(HtmlRenderResult.from_dict(result.to_dict()), result)
        content = (self.root / result.artifact.artifact_path).read_bytes()
        self.assertTrue(result.artifact.matches_bytes(content))

    def assert_refused(self, result, code):
        self.assertEqual(result.status, "refused", result)
        self.assertIsNone(result.artifact)
        self.assertEqual(result.refusal.code, code)

    def cache_snapshot(self):
        return {
            path.relative_to(self.root): (
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in (self.root / "objects").rglob("*")
        }

    def test_default_repeat_retains_original_bytes_observation_and_output_mtime(self):
        first = self.render()
        self.assert_success(first)
        content = self.output.read_bytes()
        modified = self.output.stat().st_mtime_ns
        snapshot = self.cache_snapshot()
        second = self.render()
        self.assert_success(second, "cached")
        self.assertEqual(first.artifact, second.artifact)
        self.assertEqual(self.output.read_bytes(), content)
        self.assertEqual(self.output.stat().st_mtime_ns, modified)
        self.assertEqual(snapshot, self.cache_snapshot())
        self.assertFalse(list(self.root.rglob(".litai-html-*")))

    def test_off_never_resolves_cache_and_uses_a_fresh_observation(self):
        with (
            mock.patch.object(
                html_render, "_cache", side_effect=AssertionError("cache")
            ),
            mock.patch.object(
                html_render,
                "_utc_observation",
                side_effect=[
                    "2026-09-11T00:00:00Z",
                    "2026-09-11T00:00:01Z",
                ],
            ) as observed,
        ):
            first = self.render(cache_mode="off")
            self.assert_success(first)
            second = self.render(cache_mode="off")
        self.assertEqual(observed.call_count, 2)
        self.assert_success(second)
        self.assertNotEqual(
            first.artifact.provenance.generated_at,
            second.artifact.provenance.generated_at,
        )
        self.assertFalse((self.root / "objects").exists())

    def test_read_only_miss_publishes_output_without_creating_cache(self):
        result = self.render(cache_mode="read-only")
        self.assert_success(result)
        self.assertFalse((self.root / "objects").exists())

    def test_read_only_hit_does_not_write_cache(self):
        self.assert_success(self.render())
        snapshot = self.cache_snapshot()
        with mock.patch.object(
            html_render, "ensure_cache_directory", side_effect=AssertionError("write")
        ):
            self.assert_success(self.render(cache_mode="read-only"), "cached")
        self.assertEqual(snapshot, self.cache_snapshot())

    def test_write_only_does_not_read_a_hit_and_observes_freshly(self):
        with mock.patch.object(
            html_render, "_utc_observation", return_value="2026-09-11T00:00:00Z"
        ):
            first = self.render()
        with (
            mock.patch.object(
                html_render, "_cached_artifact", side_effect=AssertionError("read")
            ),
            mock.patch.object(
                html_render, "_utc_observation", return_value="2026-09-11T00:00:01Z"
            ),
        ):
            second = self.render(cache_mode="write-only")
        self.assert_success(second)
        self.assertNotEqual(
            first.artifact.provenance.generated_at,
            second.artifact.provenance.generated_at,
        )
        self.assertEqual(self.render().artifact, second.artifact)

    def test_cache_hit_can_publish_another_relative_path_with_identical_bytes(self):
        first = self.render()
        second = self.render(output_path="views/copy.html")
        self.assert_success(second, "cached")
        self.assertEqual(
            first.artifact.artifact_identity, second.artifact.artifact_identity
        )
        self.assertEqual(
            self.output.read_bytes(), (self.root / "views/copy.html").read_bytes()
        )

    def test_stale_recognized_output_can_be_regenerated(self):
        first = self.render()
        skill = self.root / "skills/agent/SKILL.md"
        skill.write_text(skill.read_text() + "\nA changed instruction.\n")
        second = self.render()
        self.assert_success(second)
        self.assertNotEqual(
            first.artifact.provenance.render_inputs_identity,
            second.artifact.provenance.render_inputs_identity,
        )

    def test_foreign_or_edited_output_is_preserved_without_cache_creation(self):
        for content in (b"", b"<html>my handwritten report</html>"):
            with self.subTest(content=content):
                self.output.write_bytes(content)
                self.assert_refused(self.render(), "render.cache_write_forbidden")
                self.assertEqual(self.output.read_bytes(), content)
                self.assertFalse((self.root / "objects").exists())

    def test_borrowed_provenance_does_not_authorize_overwriting_edited_visible_text(
        self,
    ):
        self.assert_success(self.render())
        content = self.output.read_bytes().replace(
            b"<main>", b"<main><p>My private addition</p>", 1
        )
        self.output.write_bytes(content)
        self.assert_refused(self.render(), "render.cache_write_forbidden")
        self.assertEqual(self.output.read_bytes(), content)

    def test_source_change_at_last_publication_check_refuses_and_removes_temporary(
        self,
    ):
        original = html_publication.publish_html

        def changed(target, content, previous, *, revalidate):
            def at_boundary():
                skill = self.root / "skills/agent/SKILL.md"
                skill.write_text(skill.read_text() + "\nChanged before publication.\n")
                revalidate()

            return original(target, content, previous, revalidate=at_boundary)

        with mock.patch.object(html_render, "publish_html", side_effect=changed):
            self.assert_refused(self.render(), "render.surface_unavailable")
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.rglob(".litai-html-*")))

    def test_installation_drift_refuses_without_output(self):
        self.observer.side_effect = [
            self.framework,
            replace(self.framework, schema_catalog_release="1.2.0"),
        ]
        self.assert_refused(self.render(), "render.surface_unavailable")
        self.assertFalse(self.output.exists())

    def test_cache_write_failure_does_not_publish_output(self):
        with mock.patch.object(
            html_render.ReferenceIndex, "set", side_effect=StorageError("fixture")
        ):
            self.assert_refused(self.render(), "render.cache_write_forbidden")
        self.assertFalse(self.output.exists())

    def test_changed_cached_bytes_refuse_instead_of_becoming_a_hit(self):
        first = self.render()
        cas, _ = html_render._cache(self.root, writable=False)
        blob = html_render.BlobRef(
            first.artifact.artifact_identity.digest, first.artifact.byte_size
        )
        cas.path_for(blob).write_bytes(b"different")
        before = self.output.read_bytes()
        self.assert_refused(self.render(), "render.cache_write_forbidden")
        self.assertEqual(self.output.read_bytes(), before)

    def test_valid_cas_hashes_do_not_make_foreign_content_a_render_cache_hit(self):
        first = self.render()
        self.assert_success(first)
        before = self.output.read_bytes()
        cas, index = html_render._cache(self.root, writable=True)
        content = b"not the observed rendered HTML"
        forged = HtmlArtifact.from_bytes(
            artifact_path="graph.html",
            content=content,
            provenance=first.artifact.provenance,
            embedding=first.artifact.embedding,
        )
        cas.put_bytes(content, media_type="text/html")
        index.set(
            "html",
            first.artifact.provenance.render_inputs_identity.digest,
            cas.put_manifest(forged.to_dict()),
        )
        self.assert_refused(self.render(), "render.cache_write_forbidden")
        self.assertEqual(self.output.read_bytes(), before)

    def test_read_only_cache_checks_owner_marker_without_repairing_it(self):
        self.assert_success(self.render())
        marker = self.root / "objects/.litai-cache-root.json"
        marker.write_bytes(b"foreign marker")
        before = self.output.read_bytes()
        self.assert_refused(
            self.render(cache_mode="read-only"), "render.cache_write_forbidden"
        )
        self.assertEqual(marker.read_bytes(), b"foreign marker")
        self.assertEqual(self.output.read_bytes(), before)

    def test_output_cannot_contaminate_its_cache(self):
        self.assert_refused(
            self.render(output_path="objects/html-observability/foreign.html"),
            "render.cache_write_forbidden",
        )
        self.assertFalse((self.root / "objects").exists())

    def test_unknown_surface_view_and_forbidden_assets_return_closed_codes(self):
        for changes, code in (
            ({"surface_id": "unknown"}, "render.unsupported_surface"),
            (
                {"view": replace(self.request.view, view_id="unknown")},
                "render.unknown_view",
            ),
            (
                {"external_asset_policy": "inline-only"},
                "render.external_asset_forbidden",
            ),
        ):
            with self.subTest(changes=changes):
                self.assert_refused(self.render(**changes), code)
        self.assertFalse(self.output.exists())
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

    def test_public_read_only_cli_does_not_create_object_directory(self):
        status, envelope = self.cli("--cache-mode", "read-only")
        self.assertEqual(status, 0, envelope)
        self.assertFalse((self.root / "objects").exists())
