"""Real single-file emission, byte provenance and adversarial markup checks."""

from __future__ import annotations

import json
import unittest
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
    HtmlRenderRequest,
    HtmlView,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.project_authority_graph import project_authority_graph
from tests.support import fixtures_test_html_observability_schema as schema_tests
from tests.support.fixtures_test_html_surfaces import _graph

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
