"""Expose root-owned pins and relationship roles without admitting child catalogs."""

import json
import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.html_source_excerpts import load_source_excerpts
from literate_ai.adapters.html_surfaces import bind_authority_graph
from literate_ai.adapters.orchestration_scaffold import prepare_orchestration_scaffold
from literate_ai.cli.catalog import authority_graph_from_args
from literate_ai.contracts.html_observability import HtmlView
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_orchestration import RepositoryRelationship
from literate_ai.project_authority_graph import project_authority_graph
from literate_ai.projects import serialize_project_configuration
from tests.unit.test_repository_orchestration_contracts import authority


class OrchestrationGraphTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "super"
        self.binding = authority()
        self.materialize(self.root, self.binding)

    def materialize(self, root, binding):
        scaffold = prepare_orchestration_scaffold(
            binding, project_id="super", version="1.0.0"
        )
        for relative, content in scaffold.files:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return scaffold

    def test_graph_exposes_exact_binding_and_independent_pin_identities(self):
        graph = project_authority_graph(self.root)
        nodes = {node.node_id: node for node in graph.nodes}
        binding = dict(nodes["orchestration:super"].properties)
        self.assertEqual(binding["identity"], self.binding.identity)
        self.assertEqual(
            binding["gitmodules_identity"], self.binding.gitmodules_identity
        )
        self.assertEqual(binding["verification"], "not-checked")
        for pin in self.binding.repositories:
            node = nodes[f"gitlink:{pin.path}"]
            properties = dict(node.properties)
            self.assertEqual(node.kind, "external-repository")
            self.assertFalse(node.inherited)
            self.assertFalse(node.inheritable)
            self.assertEqual(node.project_id, "super")
            self.assertEqual(properties["gitlink_path"], pin.path)
            self.assertEqual(properties["configured_name"], pin.name)
            self.assertEqual(properties["url"], pin.url)
            self.assertEqual(properties["commit"], pin.commit)
            self.assertEqual(properties.get("branch"), pin.branch)
            self.assertEqual(
                properties["identity"], canonical_identity(pin.to_dict()).uri
            )
            self.assertEqual(properties["child_authority"], "independent")
            self.assertEqual(properties["verification"], "not-checked")
            self.assertNotIn("path", properties)

    def test_cyclic_declarations_have_roles_not_a_claimed_execution_order(self):
        relations = (
            RepositoryRelationship("services/app", "libraries/core"),
            RepositoryRelationship("libraries/core", "services/app"),
        )
        self.materialize(self.root, replace(self.binding, relationships=relations))
        graph = project_authority_graph(self.root)
        declared = [
            node for node in graph.nodes if node.kind == "repository-relationship"
        ]
        self.assertEqual(len(declared), 2)
        self.assertEqual(len(graph.topological_order()), len(graph.nodes))
        for node in declared:
            properties = dict(node.properties)
            self.assertEqual(properties["execution_order"], "not-inferred")
            roles = {
                edge.kind: edge.source
                for edge in graph.edges
                if edge.target == node.node_id
            }
            self.assertEqual(
                roles["relationship-consumer"], "gitlink:" + properties["consumer"]
            )
            self.assertEqual(
                roles["relationship-provider"], "gitlink:" + properties["provider"]
            )
        self.assertFalse(
            any(
                edge.source.startswith("gitlink:")
                and edge.target.startswith("gitlink:")
                for edge in graph.edges
            )
        )

    def test_every_configured_pin_field_and_relationship_binds_graph_identity(self):
        original = project_authority_graph(self.root).identity
        first, *rest = self.binding.repositories
        variants = [
            replace(self.binding, repositories=(replace(first, **change), *rest))
            for change in (
                {"name": "renamed"},
                {"url": "https://example.test/child.git"},
                {"commit": "d" * 40},
                {"branch": "main"},
            )
        ]
        variants.extend(
            (
                replace(self.binding, gitmodules_identity="sha256:" + "e" * 64),
                replace(self.binding, relationships=()),
            )
        )
        for binding in variants:
            with self.subTest(binding=binding.identity):
                self.materialize(self.root, binding)
                graph = project_authority_graph(self.root)
                self.assertNotEqual(graph.identity, original)
                self.assertEqual(
                    project_authority_graph(
                        self.root, include_component_locks=False
                    ).identity,
                    graph.identity,
                )

    def test_graph_and_html_excerpts_do_not_read_independent_child_files(self):
        children = [self.root / pin.path for pin in self.binding.repositories]
        for child in children:
            child.mkdir(parents=True)
            (child / "literate.project.json").write_bytes(
                b"unrelated invalid child authority"
            )
            (child / "SKILL.md").write_bytes(b"independent child instructions")
        original_open = Path.open

        def guarded(path, *args, **kwargs):
            if any(path.is_relative_to(child) for child in children):
                raise AssertionError("child content read")
            return original_open(path, *args, **kwargs)

        with (
            mock.patch.object(Path, "open", new=guarded),
            mock.patch(
                "subprocess.run", side_effect=AssertionError("child or Git execution")
            ),
        ):
            graph = project_authority_graph(self.root)
            source = bind_authority_graph(
                graph, HtmlView("dag", "1.0.0", "project", "super")
            )
            excerpts = load_source_excerpts(self.root, source)
        self.assertIsInstance(excerpts, tuple)
        for excerpt in excerpts:
            if excerpt.node_id.startswith(
                ("gitlink:", "repository-relationship:", "orchestration:")
            ):
                self.assertIsNone(excerpt.path)
                self.assertIsNone(excerpt.text)
        self.assertFalse(any(node.kind == "component" for node in graph.nodes))

    def test_graph_identity_is_independent_of_clone_location_and_child_state(self):
        original = project_authority_graph(self.root).to_dict()
        other = self.base / "clone"
        self.materialize(other, self.binding)
        for pin in self.binding.repositories:
            child = other / pin.path
            child.mkdir(parents=True)
            (child / ".git").write_bytes(b"independent metadata")
            (child / "source.txt").write_bytes(b"uncommitted child content")
        self.assertEqual(project_authority_graph(other).to_dict(), original)

    def test_public_graph_adapter_and_all_export_formats_include_exact_pins(self):
        for format_name in ("json", "text", "mermaid", "dot", "svg"):
            with self.subTest(format=format_name):
                result = authority_graph_from_args(
                    Namespace(
                        project=str(self.root),
                        format=format_name,
                        output=None,
                    )
                )
                for pin in self.binding.repositories:
                    self.assertIn(pin.commit, result["rendered"])
                if format_name == "json":
                    self.assertEqual(json.loads(result["rendered"]), result["graph"])

    def test_no_binding_preserves_the_ordinary_graph_shape(self):
        scaffold = self.materialize(self.root, self.binding)
        (self.root / "literate.project.json").write_bytes(
            serialize_project_configuration(
                replace(scaffold.definition, repository_orchestration=None)
            )
        )
        graph = project_authority_graph(self.root)
        self.assertEqual({node.kind for node in graph.nodes}, {"repository", "skill"})
        self.assertFalse(any("relationship" in edge.kind for edge in graph.edges))
