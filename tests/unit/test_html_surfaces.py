"""Authority-graph observations retain existing JSON and typed refusals."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from literate_ai.adapters.html_surfaces import (
    AUTHORITY_GRAPH_SOURCE_SCHEMA,
    HtmlSurfaceSource,
    bind_authority_graph,
    html_surfaces,
    load_html_surface,
)
from literate_ai.authority_graph import (
    AuthorityGraph,
    AuthorityGraphEdge,
    AuthorityGraphNode,
)
from literate_ai.contracts.html_observability import (
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlSurface,
    HtmlView,
)
from literate_ai.project_authority_graph import project_authority_graph
from tests.support import fixtures_test_html_observability_schema as schema_tests

ROOT = Path(__file__).resolve().parents[2]


def _view(project: str = "example") -> HtmlView:
    return HtmlView("dag", "1.0.0", "project", project)


def _request(view: HtmlView, surface: str = "authority-graph") -> HtmlRenderRequest:
    return HtmlRenderRequest(surface, view, "graph.html", "off", "inline-only")


def _graph() -> AuthorityGraph:
    return AuthorityGraph.create(
        "example",
        (
            AuthorityGraphNode(
                "repository:example", "repository", "Example", "example", "local"
            ),
            AuthorityGraphNode(
                "component:one",
                "component",
                "Ω <one>",
                "example",
                "components/one",
                properties=(("path", "components/one/component.md"),),
            ),
        ),
        (AuthorityGraphEdge("repository:example", "component:one", "defines"),),
    )


class HtmlSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v2/authority-graph.schema.json").read_text()
        )
        Draft202012Validator.check_schema(schema)
        self.graph_validator = Draft202012Validator(schema)

    def test_graph_surface_round_trips_against_the_accepted_contract(
        self,
    ) -> None:
        surfaces = html_surfaces()
        self.assertEqual(
            [item.surface_id for item in surfaces],
            [
                "authority-graph",
                "version-check",
                "lock-health",
                "verification-health",
                "performance-history",
                "workflow-routing",
            ],
        )
        surface = surfaces[0]
        self.assertEqual(surface.surface_id, "authority-graph")
        self.assertEqual(surface.source_schema, AUTHORITY_GRAPH_SOURCE_SCHEMA)
        self.assertEqual(surface.view_ids, ("dag",))
        self.assertEqual(HtmlSurface.from_dict(surface.to_dict()), surface)
        contracts = schema_tests.HtmlObservabilitySchemaTests()
        contracts.setUp()
        contracts.validator(surface.SCHEMA).validate(surface.to_dict())
        index = json.loads((ROOT / "schemas/v2/index.json").read_text())
        self.assertEqual(
            [
                entry
                for entry in index["schemas"]
                if entry["root_id"] == surface.source_schema
            ],
            [
                {
                    "file": "authority-graph.schema.json",
                    "root_id": surface.source_schema,
                    "public_ids": [],
                }
            ],
        )

    def test_surface_rejects_unknown_missing_duplicate_and_invalid_fields(self) -> None:
        surface = html_surfaces()[0]
        for field in surface.to_dict():
            value = surface.to_dict()
            del value[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                HtmlSurface.from_dict(value)
        for changes in (
            {"extra": True},
            {"surface_id": "Not Portable"},
            {"source_schema": "literate-ai/authority-graph@2"},
            {"view_ids": []},
            {"view_ids": ["dag", "dag"]},
            {"view_ids": [1]},
            {"view_ids": ["bad view"]},
            {"view_ids": [f"view-{i}" for i in range(65)]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                HtmlSurface.from_dict({**surface.to_dict(), **changes})

    def test_actual_project_matches_the_existing_cli_graph_and_rendered_bytes(
        self,
    ) -> None:
        graph = project_authority_graph(ROOT)
        observed = load_html_surface(ROOT, _request(_view(graph.project_id)))
        self.assertIsInstance(observed, HtmlSurfaceSource)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "literate_ai.cli",
                "graph",
                "--project",
                str(ROOT),
                "--format",
                "json",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["ok"])
        self.assertEqual(observed.document(), envelope["result"]["graph"])
        self.assertEqual(observed.json_bytes, envelope["result"]["rendered"].encode())
        self.assertEqual(observed.binding.source_identity.uri, graph.identity)
        self.assertNotIn("project", observed.document())
        self.assertNotIn("output", observed.document())
        self.graph_validator.validate(observed.document())

    def test_unicode_identity_and_empty_filtered_graph_keep_existing_wire(self) -> None:
        for graph in (_graph(), AuthorityGraph.create("example", (), ())):
            with self.subTest(nodes=len(graph.nodes)):
                observed = bind_authority_graph(graph, _view())
                self.assertIsInstance(observed, HtmlSurfaceSource)
                self.assertEqual(observed.document(), graph.to_dict())
                self.assertEqual(
                    observed.document()["schema"], "literate-ai/authority-graph@2"
                )
                self.assertEqual(observed.json_bytes, graph.render("json").encode())
                self.assertEqual(observed.binding.source_identity.uri, graph.identity)
                self.graph_validator.validate(observed.document())

    def test_snapshot_owns_bytes_and_identity_changes_with_real_graph_content(
        self,
    ) -> None:
        graph = _graph()
        first = bind_authority_graph(graph, _view())
        self.assertIsInstance(first, HtmlSurfaceSource)
        value = first.document()
        value["nodes"][0]["label"] = "mutated"
        surface = first.surface.to_dict()
        surface["view_ids"].append("mutated")
        self.assertEqual(first.document(), graph.to_dict())
        self.assertEqual(html_surfaces()[0].view_ids, ("dag",))
        with self.assertRaises(FrozenInstanceError):
            first.json_bytes = b"substitute"
        changed = AuthorityGraph.create(
            graph.project_id,
            (replace(node, label=node.label + " changed") for node in graph.nodes),
            graph.edges,
        )
        second = bind_authority_graph(changed, _view())
        self.assertIsInstance(second, HtmlSurfaceSource)
        self.assertNotEqual(
            first.binding.source_identity, second.binding.source_identity
        )
        self.assertNotEqual(first.json_bytes, second.json_bytes)

    def test_unknown_surface_and_view_refuse_before_loading_a_missing_project(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = [(_request(_view(), "unknown"), "render.unsupported_surface")]
            for view in (
                replace(_view(), view_id="unknown"),
                replace(_view(), view_version="2.0.0"),
                replace(_view(), scope_kind="component"),
            ):
                requests.append((_request(view), "render.unknown_view"))
            for request, code in requests:
                with self.subTest(request=request):
                    result = load_html_surface(root, request)
                    self.assertIsInstance(result, HtmlRenderRefusal)
                    self.assertEqual(result.code, code)
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(
            bind_authority_graph(_graph(), _view("other")).code, "render.unknown_view"
        )

    def test_missing_or_malformed_project_is_a_typed_read_only_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = load_html_surface(root, _request(_view()))
            self.assertEqual(missing.code, "render.surface_unavailable")
            self.assertEqual(list(root.iterdir()), [])
            manifest = root / "literate.project.json"
            manifest.write_text("not JSON")
            malformed = load_html_surface(root, _request(_view()))
            self.assertEqual(malformed.code, "render.surface_unavailable")
            self.assertEqual(manifest.read_text(), "not JSON")
            self.assertEqual(list(root.iterdir()), [manifest])

    def test_real_producer_with_invalid_primitive_fields_is_not_admitted(self) -> None:
        graph = _graph()
        for changes in (
            {"label": 5},
            {"inherited": 1},
            {"properties": (("identity", 5),)},
        ):
            with self.subTest(changes=changes):
                invalid = AuthorityGraph.create(
                    graph.project_id,
                    (replace(graph.nodes[0], **changes), *graph.nodes[1:]),
                    graph.edges,
                )
                self.assertEqual(
                    bind_authority_graph(invalid, _view()).code,
                    "render.surface_unavailable",
                )

    def test_malformed_lineage_refuses_without_modifying_project_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".literate").mkdir()
            for relative in (
                "literate.project.json",
                ".literate/repository-parent.json",
            ):
                (root / relative).write_bytes((ROOT / relative).read_bytes())
            lineage = root / ".literate/repository-lineage.json"
            lineage.write_text("not JSON")
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            result = load_html_surface(root, _request(_view("literate-ai")))
            self.assertIsInstance(result, HtmlRenderRefusal)
            self.assertEqual(result.code, "render.surface_unavailable")
            self.assertEqual(
                before,
                {
                    path.relative_to(root): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
            )

    def test_real_source_edit_rebinds_but_relocating_the_project_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "first"
            (root / ".literate").mkdir(parents=True)
            (root / "docs").mkdir()
            definition = json.loads((ROOT / "literate.project.json").read_text())
            for field in (
                "component_roots",
                "flavor_roots",
                "workflow_roots",
                "routing_roots",
            ):
                definition[field] = []
            (root / "literate.project.json").write_text(json.dumps(definition))
            for name in ("repository-parent.json", "repository-lineage.json"):
                (root / ".literate" / name).write_bytes(
                    (ROOT / ".literate" / name).read_bytes()
                )
            skill = root / "skills/agent/SKILL.md"
            skill.parent.mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "---\nname: example\ndescription: Test project instructions.\n---\n"
            )
            skill.write_text(
                "---\nname: example-agent\ndescription: Nested test.\n---\n"
            )
            request = _request(_view(definition["project_id"]))
            project_authority_graph(
                root
            )  # Fail directly if the real fixture is invalid.
            first = load_html_surface(root, request)
            self.assertIsInstance(first, HtmlSurfaceSource)
            original_bytes = first.json_bytes
            skill.write_bytes(
                skill.read_bytes() + b"\nAn additional local instruction.\n"
            )
            changed = load_html_surface(root, request)
            self.assertIsInstance(changed, HtmlSurfaceSource)
            self.assertNotEqual(
                first.binding.source_identity, changed.binding.source_identity
            )
            self.assertEqual(first.json_bytes, original_bytes)
            relocated = Path(temporary) / "second"
            root.rename(relocated)
            moved = load_html_surface(relocated, request)
            self.assertIsInstance(moved, HtmlSurfaceSource)
            self.assertEqual(changed.binding, moved.binding)
            self.assertEqual(changed.json_bytes, moved.json_bytes)
            self.assertFalse((relocated / "graph.html").exists())

    def test_schema_rejects_missing_unknown_and_invalid_nested_wire_fields(
        self,
    ) -> None:
        original = _graph().to_dict()
        self.graph_validator.validate(original)
        for path in ((), ("nodes", 0), ("edges", 0)):
            target = original
            for part in path:
                target = target[part]
            for field in (*target, "extra"):
                with self.subTest(path=path, field=field):
                    changed = copy.deepcopy(original)
                    selected = changed
                    for part in path:
                        selected = selected[part]
                    if field == "extra":
                        selected[field] = True
                    else:
                        del selected[field]
                    with self.assertRaises(ValidationError):
                        self.graph_validator.validate(changed)
        for changes in (
            {"acyclic": False},
            {"schema": AUTHORITY_GRAPH_SOURCE_SCHEMA},
            {"identity": "sha256:bad"},
            {"topological_order": ["same", "same"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.graph_validator.validate({**original, **changes})
