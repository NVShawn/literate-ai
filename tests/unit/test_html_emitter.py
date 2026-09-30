"""Real single-file emission, byte provenance and adversarial markup checks."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_emitter
from literate_ai.adapters.html_surfaces import bind_authority_graph
from literate_ai.authority_graph import (
    AuthorityGraph,
    AuthorityGraphEdge,
    AuthorityGraphNode,
)
from literate_ai.contracts.html_observability import (
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlView,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.project_authority_graph import project_authority_graph
from tests.unit import test_html_observability_schema as schema_tests
from tests.unit.test_html_surfaces import _graph

ROOT = Path(__file__).resolve().parents[2]
STAMP = "2026-09-11T00:00:00Z"
# Explicit synthetic fixture identity: these tests do not qualify an installed wheel.
DISTRIBUTION = canonical_identity({"test_fixture": "html-emitter"})


def request(
    project: str = "example", *, policy: str = "inline-only"
) -> HtmlRenderRequest:
    return HtmlRenderRequest(
        "authority-graph",
        HtmlView("dag", "1.0.0", "project", project),
        "graph.html",
        "off",
        policy,
    )


def asset(kind: str = "script") -> html_emitter.HtmlExternalAsset:
    return html_emitter.HtmlExternalAsset(
        f"fixture-{kind}",
        f"https://example.invalid/graph.{kind}?a=1&b=2",
        "sha256-" + "A" * 43 + "=",
        "anonymous",
        kind,
    )


class HtmlEmitterTests(unittest.TestCase):
    def emit(self, graph: AuthorityGraph | None = None, **kwargs):
        graph = graph if graph is not None else _graph()
        selected = kwargs.pop("request", request())
        source = bind_authority_graph(graph, selected.view)
        with mock.patch.object(html_emitter, "load_html_surface", return_value=source):
            return html_emitter._emit_html(
                ROOT,
                selected,
                framework_distribution_identity=DISTRIBUTION,
                schema_catalog_release="0.1.1",
                generated_at=kwargs.pop("generated_at", STAMP),
                **kwargs,
            )

    def test_actual_project_emits_existing_graph_and_valid_artifact(self) -> None:
        graph = project_authority_graph(ROOT)
        emitted = html_emitter._emit_html(
            ROOT,
            request(graph.project_id),
            framework_distribution_identity=DISTRIBUTION,
            schema_catalog_release="0.1.1",
            generated_at=STAMP,
        )
        self.assertIsInstance(emitted, html_emitter.HtmlEmission)
        parser = html_emitter._MarkupInspection()
        parser.feed(emitted.content.decode())
        parser.close()
        self.assertEqual(json.loads(parser.inline["litai-source"]), graph.to_dict())
        self.assertEqual(
            json.loads(parser.inline["litai-provenance"]),
            emitted.artifact.provenance.to_dict(),
        )
        schemas = schema_tests.HtmlObservabilitySchemaTests()
        schemas.setUp()
        schemas.validator(emitted.artifact.SCHEMA).validate(emitted.artifact.to_dict())
        self.assertEqual(
            emitted.artifact.provenance.source_bindings[0].source_identity.uri,
            graph.identity,
        )
        self.assertEqual(
            emitted.artifact.embedding.to_dict(),
            {
                "single_file": True,
                "companion_asset_count": 0,
                "inline_script_count": 2,
                "inline_style_count": 1,
                "external_reference_count": 0,
            },
        )

    def test_completed_byte_identity_is_outside_embedded_provenance(self) -> None:
        emitted = self.emit()
        self.assertEqual(
            hashlib.sha256(emitted.content).hexdigest(),
            emitted.artifact.artifact_identity.digest,
        )
        self.assertEqual(len(emitted.content), emitted.artifact.byte_size)
        self.assertNotIn(
            emitted.artifact.artifact_identity.digest.encode(), emitted.content
        )
        self.assertFalse(emitted.artifact.matches_bytes(emitted.content + b" "))
        with self.assertRaises(ValueError):
            replace(emitted, content=emitted.content + b" ")

    def test_fixed_inputs_are_byte_identical_and_timestamp_does_not_change_inputs(
        self,
    ) -> None:
        first, second = self.emit(), self.emit()
        later = self.emit(generated_at="2026-09-12T00:00:00Z")
        self.assertEqual(first, second)
        self.assertNotEqual(first.content, later.content)
        self.assertEqual(
            first.artifact.provenance.render_inputs_identity,
            later.artifact.provenance.render_inputs_identity,
        )
        changed = self.emit(AuthorityGraph.create("example", (), ()))
        self.assertNotEqual(
            first.artifact.provenance.render_inputs_identity,
            changed.artifact.provenance.render_inputs_identity,
        )

    def test_every_untrusted_graph_string_is_escaped_in_html_and_json(self) -> None:
        hostile = '</script><!--</style><img src=x onerror="alert(1)">&\u2028\u2029'
        graph = AuthorityGraph.create(
            "example",
            (
                AuthorityGraphNode(
                    "one",
                    hostile,
                    hostile,
                    hostile,
                    hostile,
                    properties=((hostile, hostile),),
                ),
                AuthorityGraphNode("two", "component", "Target", "example", "local"),
            ),
            (AuthorityGraphEdge("one", "two", hostile, hostile),),
        )
        emitted = self.emit(graph)
        self.assertIsInstance(emitted, html_emitter.HtmlEmission)
        parser = html_emitter._MarkupInspection()
        parser.feed(emitted.content.decode())
        parser.close()
        self.assertEqual(json.loads(parser.inline["litai-source"]), graph.to_dict())
        self.assertNotIn("<", parser.inline["litai-source"])
        self.assertNotIn("&", parser.inline["litai-source"])
        self.assertNotIn(b"<img", emitted.content)
        self.assertNotIn(b"<!--", emitted.content)
        self.assertIn(b"&lt;img", emitted.content)
        self.assertEqual(len(parser.inline), 2)

    def test_empty_graph_has_honest_readable_state(self) -> None:
        emitted = self.emit(AuthorityGraph.create("example", (), ()))
        self.assertIsInstance(emitted, html_emitter.HtmlEmission)
        self.assertIn(b"No authority nodes are present.", emitted.content)
        self.assertIn(b"No authority relationships are present.", emitted.content)

    def test_unavailable_renderer_and_invalid_observation_refuse(self) -> None:
        with mock.patch.object(
            html_emitter, "html_renderer_binding", side_effect=OSError
        ):
            refused = self.emit()
        self.assertIsInstance(refused, HtmlRenderRefusal)
        self.assertEqual(refused.code, "render.surface_unavailable")
        refused = self.emit(generated_at="2026-02-31T00:00:00Z")
        self.assertIsInstance(refused, HtmlRenderRefusal)

    def test_template_source_changes_invalidate_renderer_binding(self) -> None:
        actual = html_emitter.html_renderer_binding(DISTRIBUTION, "0.1.1")
        with mock.patch.object(Path, "read_text", return_value="changed emitter"):
            changed = html_emitter.html_renderer_binding(DISTRIBUTION, "0.1.1")
        self.assertNotEqual(actual.template_identity, changed.template_identity)

    def test_pinned_asset_references_match_declarations_and_actual_counts(self) -> None:
        assets = (asset(), asset("stylesheet"))
        emitted = self.emit(
            request=request(policy="pinned-cdn"), external_assets=assets
        )
        self.assertIsInstance(emitted, html_emitter.HtmlEmission)
        self.assertEqual(emitted.artifact.embedding.external_reference_count, 2)
        self.assertEqual(emitted.artifact.provenance.external_assets, assets)
        self.assertIn(b"?a=1&amp;b=2", emitted.content)
        parser = html_emitter._MarkupInspection()
        parser.feed(emitted.content.decode())
        self.assertEqual(tuple(parser.assets), assets)

    def test_asset_pin_url_kind_and_order_changes_invalidate_render_inputs(
        self,
    ) -> None:
        script = asset()
        variants = (
            (),
            (script,),
            (replace(script, url="https://example.invalid/other.js"),),
            (replace(script, integrity="sha256-" + "B" * 42 + "A="),),
            (asset("stylesheet"),),
            (script, asset("stylesheet")),
            (asset("stylesheet"), script),
        )
        identities = {
            self.emit(
                request=request(policy="pinned-cdn"), external_assets=assets
            ).artifact.provenance.render_inputs_identity.uri
            for assets in variants
        }
        self.assertEqual(len(identities), len(variants))

    def test_inline_only_refuses_needed_cdn_assets_and_unpinned_assets_refuse(
        self,
    ) -> None:
        forbidden = self.emit(external_assets=(asset(),))
        self.assertIsInstance(forbidden, HtmlRenderRefusal)
        self.assertEqual(forbidden.code, "render.external_asset_forbidden")
        for assets in ([asset()], ("unpinned",), (asset(), asset())):
            with self.subTest(assets=assets):
                refused = self.emit(
                    request=request(policy="pinned-cdn"), external_assets=assets
                )
                self.assertIsInstance(refused, HtmlRenderRefusal)
                self.assertEqual(refused.code, "render.external_asset_unpinned")

    def test_emitter_does_not_write_or_replace_requested_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "graph.html"
            output.write_bytes(b"user-owned")
            source = bind_authority_graph(_graph(), request().view)
            with mock.patch.object(
                html_emitter, "load_html_surface", return_value=source
            ):
                emitted = html_emitter._emit_html(
                    root,
                    request(),
                    framework_distribution_identity=DISTRIBUTION,
                    schema_catalog_release="0.1.1",
                    generated_at=STAMP,
                )
            self.assertIsInstance(emitted, html_emitter.HtmlEmission)
            self.assertEqual(output.read_bytes(), b"user-owned")
            self.assertEqual(list(root.iterdir()), [output])

    def test_unknown_and_unavailable_surfaces_refuse_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for selected, code in (
                (
                    replace(request(), surface_id="unknown"),
                    "render.unsupported_surface",
                ),
                (
                    replace(request(), view=replace(request().view, view_id="unknown")),
                    "render.unknown_view",
                ),
                (request(), "render.surface_unavailable"),
            ):
                with self.subTest(code=code):
                    refused = html_emitter._emit_html(
                        root,
                        selected,
                        framework_distribution_identity=DISTRIBUTION,
                        schema_catalog_release="0.1.1",
                        generated_at=STAMP,
                    )
                    self.assertIsInstance(refused, HtmlRenderRefusal)
                    self.assertEqual(refused.code, code)
                    self.assertEqual(list(root.iterdir()), [])

    def test_emitter_refuses_mutated_markup_before_returning_artifact(self) -> None:
        markup = html_emitter._markup
        mutations = (
            lambda b: b.replace(
                b"</head>", b'<link rel="stylesheet" href="local.css"></head>'
            ),
            lambda b: b.replace(
                b"</head>", b"<style>body{background:url(local.png)}</style></head>"
            ),
            lambda b: b.replace(b"</body>", b"<script>alert(1)</script></body>"),
            lambda b: b.replace(b"</body>", b'<img srcset="local.png 1x"></body>'),
            lambda b: b.replace(
                b"</body>", b'<iframe src="local.html"></iframe></body>'
            ),
            lambda b: b.replace(b"<main>", b'<main onclick="alert(1)">'),
            lambda b: b.replace(b'href="#catalog"', b'href="https://example.invalid"'),
            lambda b: b.replace(b'href="#catalog"', b'href="#missing"'),
            lambda b: b.replace(b'id="litai-provenance"', b'id="litai-source"'),
            lambda b: b.replace(b'"generated_at":"2026', b'"generated_at":"2025'),
            lambda b: b.replace(b"</html>", b""),
        )
        for index, mutate in enumerate(mutations):
            with (
                self.subTest(mutation=index),
                mock.patch.object(
                    html_emitter,
                    "_markup",
                    side_effect=lambda s, p, mutate=mutate: mutate(markup(s, p)),
                ),
            ):
                refused = self.emit()
                self.assertIsInstance(refused, HtmlRenderRefusal)

    def test_actual_external_references_cannot_disagree_with_pins_or_counts(
        self,
    ) -> None:
        markup = html_emitter._markup
        for old, new in (
            (b"example.invalid", b"substituted.invalid"),
            (b'crossorigin="anonymous"', b'crossorigin="use-credentials"'),
            (b"sha256-" + b"A" * 43, b"sha256-" + b"B" * 42 + b"A"),
            (b"</head>", b'<script src="companion.js"></script></head>'),
            (b"<script src=", b'<script type="module" src='),
        ):
            with (
                self.subTest(new=new),
                mock.patch.object(
                    html_emitter,
                    "_markup",
                    side_effect=lambda s, p, old=old, new=new: markup(s, p).replace(
                        old, new
                    ),
                ),
            ):
                refused = self.emit(
                    request=request(policy="pinned-cdn"), external_assets=(asset(),)
                )
                self.assertIsInstance(refused, HtmlRenderRefusal)
