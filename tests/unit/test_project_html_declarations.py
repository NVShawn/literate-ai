"""Explicit HTML inventory preserves old projects and identifies missing artifacts."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from literate_ai.contracts.html_observability import HtmlRenderRequest, HtmlView
from literate_ai.contracts.projects import (
    ProjectDefinition,
    ProjectSourceIntelligencePolicy,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)
from tests.support import fixtures_test_html_observability_schema as schema_tests


def project() -> ProjectDefinition:
    return ProjectDefinition(
        project_id="example",
        version="1.1.0",
        profile="canonical",
        agent_skill="SKILL.md",
        component_roots=(),
        flavor_roots=(),
        skill_roots=(),
        workflow_roots=(),
        routing_roots=(),
        documentation_roots=("docs",),
        source_intelligence=ProjectSourceIntelligencePolicy(
            "none",
            None,
            None,
            None,
            tuple(
                (stage, SourceIntelligenceMode.OFF) for stage in SourceIntelligenceStage
            ),
            SourceIntelligenceArtifactPublication.METADATA_ONLY,
        ),
    )


def request(path: str = "graph.html") -> HtmlRenderRequest:
    return HtmlRenderRequest(
        "authority-graph",
        HtmlView("dag", "1.0.0", "project", "example"),
        path,
        "read-only",
        "pinned-cdn",
    )


class ProjectHtmlDeclarationTests(unittest.TestCase):
    def test_omitted_and_empty_inventory_preserve_old_wire_and_identity(self):
        original = project()
        self.assertNotIn("html_render_requests", original.to_dict())
        for wire in (
            original.to_dict(),
            {**original.to_dict(), "html_render_requests": []},
        ):
            parsed = ProjectDefinition.from_dict(wire)
            self.assertEqual(parsed.html_render_requests, ())
            self.assertEqual(parsed.to_dict(), original.to_dict())
            self.assertEqual(parsed.identity, original.identity)

    def test_declared_requests_roundtrip_and_match_published_project_schema(self):
        declared = replace(project(), html_render_requests=(request(),))
        wire = json.loads(json.dumps(declared.to_dict()))
        self.assertEqual(ProjectDefinition.from_dict(wire), declared)
        self.assertNotEqual(declared.identity, project().identity)
        schemas = schema_tests.HtmlObservabilitySchemaTests()
        schemas.setUp()
        schemas.validator(ProjectDefinition.SCHEMA).validate(wire)
        self.assertEqual(wire["html_render_requests"], [request().to_dict()])

    def test_direct_construction_requires_typed_immutable_requests(self):
        for value in ([request()], (request().to_dict(),), (None,), None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(project(), html_render_requests=value)

    def test_output_paths_are_unique_even_with_different_case_or_policy(self):
        for second in (
            request(),
            request("GRAPH.html"),
            replace(request(), cache_mode="off"),
        ):
            with self.subTest(second=second), self.assertRaises(ValueError):
                replace(project(), html_render_requests=(request(), second))

    def test_inventory_has_a_fixed_maximum(self):
        requests = tuple(request(f"graph-{index}.html") for index in range(128))
        declared = replace(project(), html_render_requests=requests)
        self.assertEqual(ProjectDefinition.from_dict(declared.to_dict()), declared)
        with self.assertRaises(ValueError):
            replace(declared, html_render_requests=(*requests, request("last.html")))
        wire = declared.to_dict()
        wire["html_render_requests"].append(request("last.html").to_dict())
        with self.assertRaises(ValueError):
            ProjectDefinition.from_dict(wire)

    def test_wire_rejects_nonarrays_unknown_request_fields_and_unsafe_paths(self):
        for value in (None, {}, "graph.html", [None], [{}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ProjectDefinition.from_dict(
                    {**project().to_dict(), "html_render_requests": value}
                )
        for change in ({"extra": True}, {"output_path": "../outside.html"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ProjectDefinition.from_dict(
                    {
                        **project().to_dict(),
                        "html_render_requests": [{**request().to_dict(), **change}],
                    }
                )

    def test_parsing_cannot_alias_the_mutable_request_inventory(self):
        wire = replace(project(), html_render_requests=(request(),)).to_dict()
        parsed = ProjectDefinition.from_dict(wire)
        wire["html_render_requests"][0]["output_path"] = "changed.html"
        wire["html_render_requests"].clear()
        self.assertEqual(parsed.html_render_requests, (request(),))
