"""Effective project authority graph regressions."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.contracts import (
    CatalogImport,
    CatalogImportFile,
    CatalogImportsFile,
    CatalogImportSource,
)
from literate_ai.project_authority_graph import (
    _locked_flavor_edges,
    project_authority_graph,
    project_lineage_authority_identity,
)


class ProjectAuthorityGraphTests(unittest.TestCase):
    def test_repository_graph_labels_local_entities_and_generation_inputs(self) -> None:
        root = Path(__file__).resolve().parents[2]

        graph = project_authority_graph(root)
        by_id = {node.node_id: node for node in graph.nodes}

        self.assertFalse(by_id["component:hello-component"].inherited)
        self.assertTrue(by_id["component:hello-component"].inheritable)
        self.assertIn(
            "skill:specification-to-source/portable-application-implementation",
            by_id,
        )
        self.assertTrue(
            any(
                edge.kind == "generation-input"
                and edge.target == "component:hello-component"
                for edge in graph.edges
            )
        )
        self.assertTrue(
            all(
                edge.source in by_id and edge.target in by_id
                for edge in graph.edges
                if edge.kind == "component-dependency"
            )
        )

    def test_every_export_can_be_written_without_external_graph_tools(self) -> None:
        root = Path(__file__).resolve().parents[2]
        graph = project_authority_graph(root)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for format_name in ("json", "text", "mermaid", "dot", "svg"):
                output = directory / f"authority.{format_name}"
                output.write_text(graph.render(format_name), encoding="utf-8")
                self.assertGreater(output.stat().st_size, 0)

    def test_prelock_graph_excludes_every_existing_lock_derived_fact(self) -> None:
        root = Path(__file__).resolve().parents[2]

        graph = project_authority_graph(root, include_component_locks=False)

        self.assertFalse(any(node.kind == "flavor-revision" for node in graph.nodes))
        self.assertFalse(
            any(
                edge.kind in {"component-dependency", "flavor-selection"}
                for edge in graph.edges
            )
        )
        self.assertTrue(any(edge.kind == "generation-input" for edge in graph.edges))

    def test_prelock_graph_identity_is_independent_of_checkout_path(self) -> None:
        source_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "staging"
            second = Path(temporary) / "published"
            first.mkdir()
            for relative in (
                "literate.project.json",
                ".literate/repository-parent.json",
                ".literate/repository-lineage.json",
            ):
                destination = first / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((source_root / relative).read_bytes())
            project = json.loads((first / "literate.project.json").read_text())
            for key in (
                "component_roots",
                "flavor_roots",
                "skill_roots",
                "workflow_roots",
                "routing_roots",
            ):
                project[key] = []
            project["documentation_roots"] = ["docs"]
            (first / "docs").mkdir()
            (first / "literate.project.json").write_text(json.dumps(project))

            before = project_authority_graph(
                first, include_component_locks=False
            ).identity
            first.rename(second)
            after = project_authority_graph(
                second, include_component_locks=False
            ).identity

            self.assertEqual(before, after)

    def test_locked_flavor_selection_projects_an_exact_typed_edge(self) -> None:
        lock = {
            "nodes": [
                {
                    "revision": {
                        "coordinate": {"namespace": "example", "name": "hello"}
                    },
                    "target_flavor_selection": {
                        "slots": [
                            {
                                "slot": {"slot_id": "language"},
                                "selected": [
                                    {"flavor_revision": {"digest": "python-digest"}}
                                ],
                            }
                        ]
                    },
                }
            ]
        }

        edges = _locked_flavor_edges(
            lock,
            component_by_coordinate={("example", "hello"): "component:hello"},
            flavor_by_revision={"python-digest": "flavor:python"},
        )

        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].source, "flavor:python")
        self.assertEqual(edges[0].target, "component:hello")
        self.assertEqual(edges[0].kind, "flavor-selection")
        self.assertEqual(edges[0].label, "language")

    def test_locally_changed_import_is_a_shadowing_local_entity(self) -> None:
        source_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".literate").mkdir()
            for relative in (
                "literate.project.json",
                ".literate/repository-parent.json",
                ".literate/repository-lineage.json",
            ):
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((source_root / relative).read_bytes())
            project = json.loads((root / "literate.project.json").read_text())
            project["project_id"] = "derived"
            project["component_roots"] = ["components"]
            project["flavor_roots"] = []
            project["skill_roots"] = []
            project["workflow_roots"] = []
            project["routing_roots"] = []
            (root / "literate.project.json").write_text(json.dumps(project))
            component = root / "components" / "hello" / "component.md"
            component.parent.mkdir(parents=True)
            original = b"""---
namespace: x
version: 1.0.0
display_name: Hello
profiles: []
sample: false
provides: []
requires: []
authoring_inputs: []
workflow_definition:
  uri: workflows/default.md
routing_policy:
  uri: routing/default.json
flavor_slots: []
entrypoints: []
acceptance_contracts: []
source_dependencies: []
---
old
"""
            component.write_bytes(original)
            imported = CatalogImport(
                kind="component",
                name="hello",
                source=CatalogImportSource(
                    project_id="upstream",
                    project_identity="sha256:" + "a" * 64,
                    ref="git:https://example.invalid/upstream@" + "b" * 40,
                    transitive_ancestors=(),
                ),
                files=(
                    CatalogImportFile(
                        path="components/hello/component.md",
                        identity="sha256:" + hashlib.sha256(original).hexdigest(),
                    ),
                ),
                copied_at="2026-08-12T00:00:00+00:00",
            )
            CatalogImportsFile((imported,)).save(root)
            component.write_bytes(original.replace(b"old", b"local"))

            graph = project_authority_graph(root)
            by_id = {node.node_id: node for node in graph.nodes}

            self.assertFalse(by_id["component:hello"].inherited)
            self.assertEqual(by_id["component:hello"].provenance, "derived")
            candidate = "candidate:component:hello@upstream"
            self.assertTrue(by_id[candidate].inherited)
            self.assertIn(
                (candidate, "component:hello", "shadowed-by"),
                {(edge.source, edge.target, edge.kind) for edge in graph.edges},
            )

    def _minimal_project(self, root: Path) -> None:
        source_root = Path(__file__).resolve().parents[2]
        (root / ".literate").mkdir()
        for relative in (
            "literate.project.json",
            ".literate/repository-parent.json",
            ".literate/repository-lineage.json",
        ):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((source_root / relative).read_bytes())
        project = json.loads((root / "literate.project.json").read_text())
        project["project_id"] = "derived"
        project["component_roots"] = ["components"]
        project["flavor_roots"] = []
        project["skill_roots"] = []
        project["workflow_roots"] = []
        project["routing_roots"] = []
        (root / "literate.project.json").write_text(json.dumps(project))

    def _write_component(self, root: Path, name: str, body: bytes = b"body") -> Path:
        component = root / "components" / name / "component.md"
        component.parent.mkdir(parents=True, exist_ok=True)
        component.write_bytes(
            b"""---
namespace: x
version: 1.0.0
display_name: """
            + name.encode("utf-8")
            + b"""
profiles: []
sample: false
provides: []
requires: []
authoring_inputs: []
workflow_definition:
  uri: workflows/default.md
routing_policy:
  uri: routing/default.json
flavor_slots: []
entrypoints: []
acceptance_contracts: []
source_dependencies: []
---
"""
            + body
            + b"\n"
        )
        return component

    def test_lineage_identity_is_unaffected_by_an_unrelated_sibling_component(
        self,
    ) -> None:
        """Regression for #36: a sibling Component's content must not invalidate

        another Component's lock. The lock's own authorings/flavors already bind
        exactly what it depends on; only repository lineage is legitimately
        project-wide.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._minimal_project(root)
            self._write_component(root, "alpha")
            self._write_component(root, "beta", body=b"original")

            before = project_lineage_authority_identity(root)

            self._write_component(root, "beta", body=b"a completely different body")
            after_sibling_edit = project_lineage_authority_identity(root)

            self.assertEqual(before, after_sibling_edit)

            (root / "components" / "gamma").mkdir()
            self._write_component(root, "gamma")
            after_new_sibling = project_lineage_authority_identity(root)

            self.assertEqual(before, after_new_sibling)

    def test_lineage_identity_changes_when_repository_state_changes(self) -> None:
        """The lineage identity is not a frozen constant: it is sensitive to the

        project's own repository-level identity (here, project_id, which the
        repository node's label/provenance are derived from), distinguishing this
        from a no-op that would make the #36 fix meaningless.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._minimal_project(root)
            self._write_component(root, "alpha")

            before = project_lineage_authority_identity(root)

            manifest_path = root / "literate.project.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["project_id"] = "derived-renamed"
            manifest_path.write_text(json.dumps(manifest))

            after = project_lineage_authority_identity(root)

            self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
