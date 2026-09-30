"""Complete DAG emission binds the library, inline controls and exact source bytes."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters import html_dag_view, html_emitter, html_source_excerpts
from literate_ai.contracts.html_observability import HtmlRenderRefusal
from literate_ai.project_authority_graph import project_authority_graph
from tests.unit import test_html_observability_schema as schema_tests
from tests.unit.test_html_emitter import DISTRIBUTION, ROOT, STAMP, request
from tests.unit.test_html_source_excerpts import source_for


class HtmlDagViewTests(unittest.TestCase):
    def emit(self, root: Path = ROOT, selected=None):
        return html_emitter.emit_html(
            root,
            selected or request("literate-ai", policy="pinned-cdn"),
            framework_distribution_identity=DISTRIBUTION,
            schema_catalog_release="0.1.1",
            generated_at=STAMP,
        )

    def test_real_project_has_one_pinned_library_and_four_exact_inline_blocks(self):
        emitted = self.emit()
        self.assertIsInstance(emitted, html_emitter.HtmlEmission)
        parser = html_emitter._MarkupInspection()
        parser.feed(emitted.content.decode())
        parser.close()
        self.assertEqual(tuple(parser.assets), (html_dag_view.CYTOSCAPE,))
        self.assertEqual(parser.inline["litai-dag"], html_dag_view.DAG_SCRIPT)
        self.assertEqual(emitted.artifact.embedding.inline_script_count, 4)
        self.assertEqual(emitted.artifact.embedding.inline_style_count, 1)
        self.assertEqual(emitted.artifact.embedding.external_reference_count, 1)
        self.assertEqual(emitted.artifact.embedding.companion_asset_count, 0)
        graph = project_authority_graph(ROOT)
        self.assertEqual(json.loads(parser.inline["litai-source"]), graph.to_dict())
        self.assertEqual(
            emitted.artifact.provenance.source_bindings[0].source_identity.uri,
            graph.identity,
        )
        previews = json.loads(parser.inline["litai-excerpts"])
        self.assertEqual(len(previews), len(graph.nodes))
        self.assertGreater(sum(item["text"] is not None for item in previews), 100)
        self.assertIn(b"Read verified source preview", emitted.content)
        self.assertIn(b"defer></script>", emitted.content)
        self.assertEqual(
            hashlib.sha256(emitted.content).hexdigest(),
            emitted.artifact.artifact_identity.digest,
        )
        schemas = schema_tests.HtmlObservabilitySchemaTests()
        schemas.setUp()
        schemas.validator(emitted.artifact.SCHEMA).validate(emitted.artifact.to_dict())

    def test_nested_start_and_repeated_render_preserve_exact_bytes(self):
        self.assertEqual(self.emit(), self.emit(ROOT / "components"))
        self.assertEqual(self.emit(), self.emit())

    def test_private_static_assembly_cannot_share_the_complete_view_identity(self):
        complete = self.emit()
        static = html_emitter._emit_html(
            ROOT,
            request("literate-ai", policy="pinned-cdn"),
            framework_distribution_identity=DISTRIBUTION,
            schema_catalog_release="0.1.1",
            generated_at=STAMP,
            external_assets=(html_dag_view.CYTOSCAPE,),
        )
        self.assertNotEqual(
            complete.artifact.provenance.render_inputs_identity,
            static.artifact.provenance.render_inputs_identity,
        )
        self.assertEqual(
            complete.artifact.provenance.renderer,
            html_emitter.html_renderer_binding(
                DISTRIBUTION, "0.1.1", (html_dag_view.CYTOSCAPE,)
            ),
        )

    def test_inline_only_refuses_instead_of_silently_substituting_static_view(self):
        with mock.patch.object(html_source_excerpts, "load_source_excerpts") as read:
            refused = self.emit(selected=request("literate-ai"))
        self.assertIsInstance(refused, HtmlRenderRefusal)
        self.assertEqual(refused.code, "render.external_asset_forbidden")
        read.assert_not_called()

    def test_hostile_source_is_embedded_as_data_and_native_escaped_preview(self):
        content = '</script><!--<img src=x onerror="alert(1)">\u2028&'.encode()
        source = source_for({"source.md": content})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.md").write_bytes(content)
            with (
                mock.patch.object(
                    html_emitter, "load_html_surface", return_value=source
                ),
                mock.patch.object(
                    html_emitter,
                    "discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
            ):
                emitted = self.emit(root, request(policy="pinned-cdn"))
            self.assertIsInstance(emitted, html_emitter.HtmlEmission)
            parser = html_emitter._MarkupInspection()
            parser.feed(emitted.content.decode())
            preview = json.loads(parser.inline["litai-excerpts"])[0]
            self.assertEqual(preview["text"], content.decode())
            self.assertNotIn("<", parser.inline["litai-excerpts"])
            self.assertNotIn(b"<img", emitted.content)
            self.assertIn(b"&lt;img", emitted.content)
            self.assertEqual((root / "source.md").read_bytes(), content)
            self.assertFalse((root / "graph.html").exists())

    def test_source_drift_and_disappearing_project_refuse_before_markup(self):
        source = source_for({"source.md": b"expected"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.md").write_bytes(b"changed")
            for loaded in (None, SimpleNamespace(root=root)):
                with (
                    self.subTest(loaded=loaded),
                    mock.patch.object(
                        html_emitter, "load_html_surface", return_value=source
                    ),
                    mock.patch.object(
                        html_emitter, "discover_project", return_value=loaded
                    ),
                    mock.patch.object(html_emitter, "_markup") as markup,
                ):
                    refused = self.emit(root, request(policy="pinned-cdn"))
                self.assertIsInstance(refused, HtmlRenderRefusal)
                self.assertEqual(refused.code, "render.surface_unavailable")
                markup.assert_not_called()

    def test_all_projection_modules_and_asset_pin_bind_template_identity(self):
        assets = (html_dag_view.CYTOSCAPE,)
        original = html_emitter.html_renderer_binding(DISTRIBUTION, "0.1.1", assets)
        read = Path.read_text
        for module in (html_emitter, html_dag_view, html_source_excerpts):
            selected = Path(module.__file__)
            with self.subTest(module=module.__name__):
                with mock.patch.object(
                    Path,
                    "read_text",
                    side_effect=lambda path, *args, selected=selected, **kwargs: (
                        "changed" if path == selected else read(path, *args, **kwargs)
                    ),
                    autospec=True,
                ):
                    changed = html_emitter.html_renderer_binding(
                        DISTRIBUTION, "0.1.1", assets
                    )
                self.assertNotEqual(
                    original.template_identity, changed.template_identity
                )
        changed = html_emitter.html_renderer_binding(
            DISTRIBUTION,
            "0.1.1",
            (replace(assets[0], url="https://example.invalid/pin.js"),),
        )
        self.assertNotEqual(original.template_identity, changed.template_identity)

    def test_mutated_inline_controls_excerpts_and_library_references_refuse(self):
        markup = html_emitter._markup
        replacements = (
            (b'"use strict";', b'"use strict"; alert(1);'),
            (b'"text":', b'"altered_text":'),
            (b"cdn.jsdelivr.net", b"substituted.invalid"),
            (b" defer></script>", b"></script>"),
            (b'type="text/javascript"', b'type="module"'),
            (b"</body>", b'<script src="companion.js"></script></body>'),
        )
        for old, new in replacements:
            with self.subTest(mutation=new):
                with mock.patch.object(
                    html_emitter,
                    "_markup",
                    side_effect=lambda *args, old=old, new=new: markup(*args).replace(
                        old, new
                    ),
                ):
                    refused = self.emit()
                self.assertIsInstance(refused, HtmlRenderRefusal)


if __name__ == "__main__":
    unittest.main()
