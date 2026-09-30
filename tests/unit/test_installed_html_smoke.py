"""Qualification-oracle tests; synthetic records are not installed-wheel proof."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.contracts.html_observability import (
    HtmlArtifact,
    HtmlEmbedding,
    HtmlProvenance,
    HtmlRendererBinding,
    HtmlRenderRefusal,
    HtmlRenderResult,
    HtmlSourceBinding,
    HtmlView,
)
from literate_ai.contracts.identity import canonical_identity
from scripts.installed_html_smoke import (
    EmbeddedDocuments,
    inspect_artifact,
    qualify,
    run_command,
)
from scripts.wheel_smoke import _retain_qualified_html, read_output


class InstalledHtmlSmokeTests(unittest.TestCase):
    def setUp(self):
        self.distribution = canonical_identity({"fixture": "wheel-smoke-oracle"})
        self.graph = {
            "project_id": "example",
            "identity": canonical_identity({"fixture": "graph"}).uri,
            "nodes": [{"id": "example"}],
            "edges": [{"source": "example", "target": "skill"}],
        }
        self.provenance = HtmlProvenance.create(
            view=HtmlView("dag", "1.0.0", "project", "example"),
            source_bindings=(
                HtmlSourceBinding(
                    "authority-graph",
                    "urn:literate-ai:schema:v2:authority-graph",
                    canonical_identity({"fixture": "graph"}),
                ),
            ),
            renderer=HtmlRendererBinding(
                "html-observability",
                "1.0.0",
                self.distribution,
                "1.1.0",
                canonical_identity({"fixture": "template"}),
            ),
            external_assets=(),
            generated_at="2026-09-11T00:00:00Z",
        )
        self.content = (
            '<script type="application/json" id="litai-source">'
            + json.dumps(self.graph)
            + '</script><script type="application/json" id="litai-provenance">'
            + json.dumps(self.provenance.to_dict())
            + "</script>"
        ).encode()

    def artifact(self, content=None, output="graph.html"):
        return HtmlArtifact.from_bytes(
            artifact_path=output,
            content=self.content if content is None else content,
            provenance=self.provenance,
            embedding=HtmlEmbedding(True, 0, 2, 0, 0),
        ).to_dict()

    def inspect(self, content=None, artifact=None, **changes):
        inspect_artifact(
            self.artifact() if artifact is None else artifact,
            self.content if content is None else content,
            graph=changes.get("graph", self.graph),
            distribution=changes.get("distribution", self.distribution.uri),
        )

    def test_timeout_retains_last_child_stage_at_bounded_html_deadline(self):
        environment = {"OBJ_DIR": "objects"}
        for diagnostic in (
            b'{"stage":"html.framework.distribution"}\n',
            "stage: html.emit\n",
        ):
            with (
                self.subTest(diagnostic=diagnostic),
                mock.patch(
                    "scripts.installed_html_smoke.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(
                        "litai", 300, stderr=diagnostic
                    ),
                ) as run,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "last diagnostic output"
                ) as raised:
                    run_command(
                        Path("litai"), Path("project"), environment, "render", "html"
                    )
                self.assertIn("html.", str(raised.exception))
                self.assertEqual(run.call_args.kwargs["timeout"], 300)
                self.assertEqual(run.call_args.kwargs["env"]["LITAI_DEBUG"], "1")
                self.assertEqual(environment, {"OBJ_DIR": "objects"})

    def test_matching_bound_observations_pass(self):
        self.inspect()

    def test_byte_drift_fails(self):
        with self.assertRaisesRegex(RuntimeError, "bytes do not match"):
            self.inspect(content=self.content + b"edited")

    def test_another_wheel_identity_fails(self):
        with self.assertRaisesRegex(RuntimeError, "installed wheel and graph"):
            self.inspect(distribution=canonical_identity({"fixture": "other"}).uri)

    def test_another_canonical_graph_fails(self):
        with self.assertRaisesRegex(RuntimeError, "canonical graph"):
            self.inspect(graph={**self.graph, "nodes": []})

    def test_rehashed_missing_provenance_fails(self):
        content = self.content.split(b"</script>")[0] + b"</script>"
        with self.assertRaisesRegex(RuntimeError, "omitted"):
            self.inspect(content=content, artifact=self.artifact(content))

    def test_rehashed_changed_embedded_provenance_fails(self):
        content = self.content.replace(b"00:00:00Z", b"00:00:01Z")
        with self.assertRaisesRegex(RuntimeError, "provenance differs"):
            self.inspect(content=content, artifact=self.artifact(content))

    def test_rehashed_duplicate_provenance_fails(self):
        content = self.content + self.content
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            self.inspect(content=content, artifact=self.artifact(content))

    def test_external_or_executable_provenance_is_rejected(self):
        for attributes in ('src="outside.js"', 'type="text/javascript"'):
            with (
                self.subTest(attributes=attributes),
                self.assertRaisesRegex(RuntimeError, "ambiguous"),
            ):
                EmbeddedDocuments().feed(
                    f'<script id="litai-provenance" {attributes}>{{}}</script>'
                )

    def exercise_cli(self, *, drift=False, wrong_refusal_exit=False):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "created"
            project.mkdir()
            cached = False

            def run(arguments, **kwargs):
                nonlocal cached
                self.assertNotIn("PYTHONPATH", kwargs["env"])
                self.assertEqual(kwargs["cwd"], project.parent)
                if "graph" in arguments:
                    (project / kwargs["env"]["OBJ_DIR"]).mkdir()
                    command, result, status = "graph", {"graph": self.graph}, 0
                else:
                    command, status = "render.html", 0
                    if "--cache-mode" not in arguments:
                        codes = {
                            "--surface": "render.unsupported_surface",
                            "--view": "render.unknown_view",
                            "--external-asset-policy": (
                                "render.external_asset_forbidden"
                            ),
                        }
                        code = next(
                            (value for key, value in codes.items() if key in arguments),
                            "render.output_outside_project",
                        )
                        result = {
                            "schema": HtmlRenderResult.SCHEMA,
                            "status": "refused",
                            "artifact": None,
                            "refusal": {
                                "schema": HtmlRenderRefusal.SCHEMA,
                                "code": code,
                                "message": "Synthetic refusal.",
                            },
                        }
                        status = 0 if wrong_refusal_exit else 1
                    else:
                        mode = arguments[arguments.index("--cache-mode") + 1]
                        output = arguments[arguments.index("--output") + 1]
                        hit = cached and mode in ("read-write", "read-only")
                        if not hit:
                            (project / output).write_bytes(self.content)
                        if mode in ("read-write", "write-only"):
                            cache = project / "wheel-html-objects"
                            cache.mkdir(exist_ok=True)
                            if not hit or drift:
                                (cache / "record").write_bytes(
                                    self.content
                                    + (b"changed" if hit and drift else b"")
                                )
                            cached = True
                        result = {
                            "schema": HtmlRenderResult.SCHEMA,
                            "status": "cached" if hit else "rendered",
                            "artifact": self.artifact(output=output),
                            "refusal": None,
                        }
                return subprocess.CompletedProcess(
                    arguments,
                    status,
                    json.dumps(
                        {
                            "schema": "literate-ai/cli-result@1",
                            "ok": True,
                            "command": command,
                            "result": result,
                        }
                    ),
                    "",
                )

            with mock.patch(
                "scripts.installed_html_smoke.subprocess.run", side_effect=run
            ):
                return qualify(Path("litai"), project, self.distribution.uri)

    def test_full_probe_exercises_cache_modes_and_nonzero_refusals(self):
        evidence = self.exercise_cli()
        self.assertEqual(len(evidence["cache_modes"]), 4)
        self.assertEqual(len(evidence["refusals"]), 4)
        self.assertEqual(evidence["post_update_browser_acceptance"], "not-exercised")

    def test_probe_rejects_cache_writes_on_a_hit(self):
        with self.assertRaisesRegex(RuntimeError, "cache hit changed"):
            self.exercise_cli(drift=True)

    def test_probe_rejects_zero_exit_even_with_a_typed_refusal(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected exit"):
            self.exercise_cli(wrong_refusal_exit=True)

    def test_retained_html_and_record_survive_source_removal_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            source = project / "graph.html"
            source.write_bytes(self.content)
            first = _retain_qualified_html(root, project, self.artifact())
            second = _retain_qualified_html(root, project, self.artifact())
            self.assertNotEqual(first, second)
            source.unlink()
            for result in (first, second):
                self.assertEqual((root / result["html"]).read_bytes(), self.content)
                self.assertEqual(
                    json.loads((root / result["record"]).read_bytes()), self.artifact()
                )
                self.assertFalse(Path(result["html"]).is_absolute())
            self.assertFalse(list(root.rglob("manifest.json")))

    def test_retention_rejects_missing_or_changed_source_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            for content in (None, b"changed"):
                if content is not None:
                    (project / "graph.html").write_bytes(content)
                with (
                    self.subTest(content=content),
                    self.assertRaisesRegex(
                        RuntimeError, "changed during qualification"
                    ),
                ):
                    _retain_qualified_html(root, project, self.artifact())
                self.assertFalse((root / "_build/ci-html").exists())

    def test_changed_copy_observation_does_not_publish_an_artifact_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            source = project / "graph.html"
            source.write_bytes(self.content)
            snapshot = read_output(source)
            with (
                mock.patch(
                    "scripts.wheel_smoke.read_output",
                    side_effect=[snapshot, replace(snapshot, content=b"changed copy")],
                ),
                self.assertRaisesRegex(RuntimeError, "changed during retention"),
            ):
                _retain_qualified_html(root, project, self.artifact())
            self.assertFalse(list(root.rglob("artifact.json")))

    def test_changed_record_observation_refuses_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            source = project / "graph.html"
            source.write_bytes(self.content)
            snapshot = read_output(source)
            with (
                mock.patch(
                    "scripts.wheel_smoke.read_output",
                    side_effect=[
                        snapshot,
                        snapshot,
                        replace(snapshot, content=b"changed record"),
                    ],
                ),
                self.assertRaisesRegex(RuntimeError, "artifact record changed"),
            ):
                _retain_qualified_html(root, project, self.artifact())
            self.assertFalse(list(root.rglob("manifest.json")))

    def test_retention_rejects_linked_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / "graph.html").write_bytes(self.content)
            outside = root / "outside"
            outside.mkdir()
            try:
                (root / "_build").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                _retain_qualified_html(root, project, self.artifact())
            self.assertEqual(list(outside.iterdir()), [])

    def test_retention_precedes_qualified_wheel_manifest(self):
        source = (
            Path(__file__).resolve().parents[2] / "scripts/wheel_smoke.py"
        ).read_text()
        self.assertLess(
            source.index('html_observability["retained"] = _retain_qualified_html('),
            source.index("retained_manifest = _retain_qualified_wheel("),
        )
