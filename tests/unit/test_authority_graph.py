"""Canonical authority graph solver and exporter regressions."""

from __future__ import annotations

import json
import random
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from literate_ai.authority_graph import (
    AuthorityGraph,
    AuthorityGraphEdge,
    AuthorityGraphError,
    AuthorityGraphNode,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _node(node_id: str, *, inherited: bool = False) -> AuthorityGraphNode:
    return AuthorityGraphNode(
        node_id=node_id,
        kind="project" if node_id.startswith("repo:") else "component",
        label=node_id.split(":", 1)[1],
        project_id="downstream",
        provenance="parent" if inherited else "downstream",
        inherited=inherited,
        inheritable=True,
    )


class AuthorityGraphTests(unittest.TestCase):
    @staticmethod
    def _golden_graph() -> AuthorityGraph:
        return AuthorityGraph.create(
            "child",
            (
                AuthorityGraphNode(
                    "repository:root",
                    "repository",
                    "Root",
                    "root",
                    "https://example.test/root.git",
                    inherited=True,
                    inheritable=True,
                    properties=(("identity", "sha256:" + "a" * 64),),
                ),
                AuthorityGraphNode(
                    "repository:child",
                    "repository",
                    "Child",
                    "child",
                    "https://example.test/child.git",
                    inheritable=True,
                    properties=(("identity", "sha256:" + "b" * 64),),
                ),
                AuthorityGraphNode(
                    "component:app",
                    "component",
                    "App",
                    "child",
                    "components/app",
                    inheritable=True,
                    properties=(("identity", "sha256:" + "c" * 64),),
                ),
            ),
            (
                AuthorityGraphEdge(
                    "repository:root", "repository:child", "repository-parent"
                ),
                AuthorityGraphEdge(
                    "repository:child",
                    "component:app",
                    "defines",
                    "components/app/component.md",
                ),
            ),
        )

    def test_solves_provenance_dag_and_exports_every_portable_format(self) -> None:
        graph = AuthorityGraph.create(
            "downstream",
            (_node("component:app"), _node("repo:root", inherited=True)),
            (AuthorityGraphEdge("repo:root", "component:app", "provides"),),
        )

        self.assertEqual(graph.topological_order(), ("repo:root", "component:app"))
        self.assertEqual(graph.ancestors("component:app"), ("repo:root",))
        self.assertEqual(graph.descendants("repo:root"), ("component:app",))
        self.assertEqual(json.loads(graph.render("json"))["acyclic"], True)
        self.assertIn("component app [local, inheritable]", graph.render("text"))
        self.assertIn("flowchart LR", graph.render("mermaid"))
        self.assertIn("digraph literate_ai", graph.render("dot"))
        self.assertIn("<svg", graph.render("svg"))

    def test_rejects_cycles_with_stable_diagnostic(self) -> None:
        with self.assertRaises(AuthorityGraphError) as raised:
            AuthorityGraph.create(
                "downstream",
                (_node("component:a"), _node("component:b")),
                (
                    AuthorityGraphEdge("component:a", "component:b", "requires"),
                    AuthorityGraphEdge("component:b", "component:a", "requires"),
                ),
            )

        self.assertEqual(raised.exception.code, "graph.cycle")
        self.assertEqual(
            raised.exception.message,
            "directed cycle detected: component:a -> component:b -> component:a",
        )

    def test_filtering_preserves_only_edges_closed_over_selected_nodes(self) -> None:
        graph = AuthorityGraph.create(
            "downstream",
            (
                _node("repo:root", inherited=True),
                _node("component:public"),
                AuthorityGraphNode(
                    "component:private",
                    "component",
                    "private",
                    "downstream",
                    "downstream",
                    inheritable=False,
                ),
            ),
            (
                AuthorityGraphEdge("repo:root", "component:public", "defines"),
                AuthorityGraphEdge("component:public", "component:private", "requires"),
            ),
        )

        view = graph.filtered(kinds=("component",), inheritance="inheritable")

        self.assertEqual(
            tuple(node.node_id for node in view.nodes), ("component:public",)
        )
        self.assertEqual(view.edges, ())

    def test_topological_layers_group_independent_nodes_before_consumers(self) -> None:
        graph = AuthorityGraph.create(
            "layers",
            (_node("component:a"), _node("component:b"), _node("component:c")),
            (
                AuthorityGraphEdge("component:a", "component:c", "requires"),
                AuthorityGraphEdge("component:b", "component:c", "requires"),
            ),
        )

        self.assertEqual(
            graph.topological_layers(),
            (("component:a", "component:b"), ("component:c",)),
        )

    def test_rejects_unresolved_and_duplicate_node_authority(self) -> None:
        with self.assertRaises(AuthorityGraphError) as unresolved:
            AuthorityGraph.create(
                "downstream",
                (_node("component:a"),),
                (AuthorityGraphEdge("component:a", "component:missing", "requires"),),
            )
        self.assertEqual(unresolved.exception.code, "graph.edge_unresolved")

        with self.assertRaises(AuthorityGraphError) as duplicate:
            AuthorityGraph.create(
                "downstream",
                (_node("component:a"), _node("component:a")),
                (),
            )
        self.assertEqual(duplicate.exception.code, "graph.nodes_noncanonical")

    def test_rebalance_only_recommends_exact_duplicates_at_common_ancestor(
        self,
    ) -> None:
        shared = (("identity", "sha256:" + "a" * 64),)
        repositories = tuple(
            AuthorityGraphNode(
                f"repository:{name}",
                "repository",
                name,
                name,
                name,
            )
            for name in ("root", "product-a", "product-b")
        )
        graph = AuthorityGraph.create(
            "product-b",
            (
                *repositories,
                AuthorityGraphNode(
                    "component:a/shared",
                    "component",
                    "shared",
                    "product-a",
                    "product-a",
                    properties=shared,
                ),
                AuthorityGraphNode(
                    "component:b/shared",
                    "component",
                    "shared",
                    "product-b",
                    "product-b",
                    properties=shared,
                ),
            ),
            (
                AuthorityGraphEdge(
                    "repository:root", "repository:product-a", "repository-parent"
                ),
                AuthorityGraphEdge(
                    "repository:root", "repository:product-b", "repository-parent"
                ),
            ),
        )

        recommendation = graph.rebalance_recommendations()[0]
        self.assertEqual(recommendation["basis"], "exact-content-identity")
        self.assertEqual(recommendation["lowest_common_ancestor"], "repository:root")
        self.assertEqual(
            recommendation["affected_repository_descendants"],
            ["repository:product-a", "repository:product-b"],
        )
        self.assertEqual(
            recommendation["context_cost"],
            {
                "duplicate_authority_nodes": 2,
                "repository_copies": 2,
                "estimated_copies_avoided": 1,
            },
        )
        self.assertTrue(recommendation["risks"])
        self.assertFalse(recommendation["automatic_action"])

    def test_diamond_and_arbitrary_depth_are_deterministic_for_input_order(
        self,
    ) -> None:
        nodes = tuple(
            _node(f"component:{name}")
            for name in ("root", "left", "right", "join", "leaf")
        )
        edges = (
            AuthorityGraphEdge("component:root", "component:left", "requires"),
            AuthorityGraphEdge("component:root", "component:right", "requires"),
            AuthorityGraphEdge("component:left", "component:join", "requires"),
            AuthorityGraphEdge("component:right", "component:join", "requires"),
            AuthorityGraphEdge("component:join", "component:leaf", "requires"),
        )
        expected = AuthorityGraph.create("diamond", nodes, edges)

        self.assertEqual(
            expected.topological_layers(),
            (
                ("component:root",),
                ("component:left", "component:right"),
                ("component:join",),
                ("component:leaf",),
            ),
        )
        self.assertEqual(
            expected.ancestors("component:leaf"),
            (
                "component:join",
                "component:left",
                "component:right",
                "component:root",
            ),
        )
        randomizer = random.Random(7)
        for _ in range(32):
            shuffled_nodes = list(nodes)
            shuffled_edges = list(edges)
            randomizer.shuffle(shuffled_nodes)
            randomizer.shuffle(shuffled_edges)
            observed = AuthorityGraph.create("diamond", shuffled_nodes, shuffled_edges)
            self.assertEqual(observed.to_dict(), expected.to_dict())
            self.assertEqual(observed.render("json"), expected.render("json"))

    def test_self_direct_and_transitive_cycles_report_minimal_paths(self) -> None:
        with self.assertRaises(AuthorityGraphError) as self_cycle:
            AuthorityGraph.create(
                "cycles",
                (_node("component:a"),),
                (AuthorityGraphEdge("component:a", "component:a", "requires"),),
            )
        self.assertEqual(
            self_cycle.exception.message, "self-cycle detected at component:a"
        )

        with self.assertRaises(AuthorityGraphError) as direct:
            AuthorityGraph.create(
                "cycles",
                tuple(_node(f"component:{name}") for name in "abcd"),
                (
                    AuthorityGraphEdge("component:a", "component:b", "requires"),
                    AuthorityGraphEdge("component:b", "component:a", "requires"),
                    AuthorityGraphEdge("component:a", "component:c", "requires"),
                    AuthorityGraphEdge("component:c", "component:d", "requires"),
                    AuthorityGraphEdge("component:d", "component:a", "requires"),
                ),
            )
        self.assertEqual(
            direct.exception.message,
            "directed cycle detected: component:a -> component:b -> component:a",
        )

        with self.assertRaises(AuthorityGraphError) as transitive:
            AuthorityGraph.create(
                "cycles",
                tuple(_node(f"component:{name}") for name in "abc"),
                (
                    AuthorityGraphEdge("component:a", "component:b", "requires"),
                    AuthorityGraphEdge("component:b", "component:c", "requires"),
                    AuthorityGraphEdge("component:c", "component:a", "requires"),
                ),
            )
        self.assertEqual(
            transitive.exception.message,
            "directed cycle detected: component:a -> component:b -> "
            "component:c -> component:a",
        )

    def test_every_export_preserves_the_same_nodes_edges_and_labels(self) -> None:
        graph = AuthorityGraph.create(
            "exports",
            (
                AuthorityGraphNode(
                    "repository:root",
                    "repository",
                    "Root & shared",
                    "root",
                    "root",
                ),
                AuthorityGraphNode(
                    "component:app",
                    "component",
                    "App <portable>",
                    "child",
                    "components/app",
                    inheritable=True,
                ),
            ),
            (
                AuthorityGraphEdge(
                    "repository:root",
                    "component:app",
                    "repository-parent",
                    "inherits & pins",
                ),
            ),
        )
        document = graph.to_dict()
        self.assertEqual(json.loads(graph.render("json")), document)
        for node in document["nodes"]:
            self.assertIn(node["label"], graph.render("text"))
            self.assertIn(node["id"], graph.render("dot"))
            self.assertIn(graph._graph_id(node["id"]), graph.render("mermaid"))

        root = ET.fromstring(graph.render("svg"))
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        svg_nodes = root.findall("svg:g[@class='node']", namespace)
        svg_edges = root.findall("svg:g[@class='edge']", namespace)
        self.assertEqual(
            {item.attrib["data-id"] for item in svg_nodes},
            {item["id"] for item in document["nodes"]},
        )
        self.assertEqual(
            {
                (
                    item.attrib["data-source"],
                    item.attrib["data-target"],
                    item.attrib["data-kind"],
                    item.attrib["data-label"],
                )
                for item in svg_edges
            },
            {
                (item["source"], item["target"], item["kind"], item["label"])
                for item in document["edges"]
            },
        )

    def test_rebalance_is_noop_for_pyramid_and_rejects_lookalike_authority(
        self,
    ) -> None:
        repositories = tuple(
            AuthorityGraphNode(f"repository:{name}", "repository", name, name, name)
            for name in ("root", "left", "right")
        )
        graph = AuthorityGraph.create(
            "right",
            (
                *repositories,
                AuthorityGraphNode(
                    "component:root/shared",
                    "component",
                    "shared",
                    "root",
                    "root",
                    properties=(("identity", "sha256:" + "a" * 64),),
                ),
                AuthorityGraphNode(
                    "component:left/lookalike",
                    "component",
                    "shared",
                    "left",
                    "left",
                    properties=(("identity", "sha256:" + "b" * 64),),
                ),
            ),
            (
                AuthorityGraphEdge(
                    "repository:root", "repository:left", "repository-parent"
                ),
                AuthorityGraphEdge(
                    "repository:root", "repository:right", "repository-parent"
                ),
            ),
        )

        self.assertEqual(graph.rebalance_recommendations(), ())

    def test_empty_filtered_graph_exports_in_every_format(self) -> None:
        graph = AuthorityGraph.create(
            "empty-view", (_node("component:only"),), ()
        ).filtered(kinds=("skill",))

        self.assertEqual(graph.nodes, ())
        self.assertEqual(graph.edges, ())
        self.assertEqual(json.loads(graph.render("json"))["nodes"], [])
        self.assertEqual(graph.render("text"), "project empty-view\n")
        self.assertEqual(graph.render("mermaid"), "flowchart LR\n")
        self.assertIn("digraph literate_ai", graph.render("dot"))
        ET.fromstring(graph.render("svg"))

    def test_golden_exporters_are_exact_and_svg_is_structurally_equivalent(
        self,
    ) -> None:
        graph = self._golden_graph()
        identity = graph.identity
        self.assertRegex(identity, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(graph.identity, identity)
        changed = AuthorityGraph.create(
            graph.project_id,
            graph.nodes,
            (
                *graph.edges,
                AuthorityGraphEdge("repository:root", "component:app", "owns"),
            ),
        )
        self.assertNotEqual(changed.identity, identity)
        for format_name, suffix in (
            ("json", "json"),
            ("text", "txt"),
            ("mermaid", "mmd"),
            ("dot", "dot"),
        ):
            with self.subTest(format=format_name):
                self.assertEqual(
                    graph.render(format_name),
                    (FIXTURES / f"authority-graph-golden.{suffix}").read_text(
                        encoding="utf-8"
                    ),
                )
        document = json.loads(graph.render("json"))
        root = ET.fromstring(graph.render("svg"))
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        self.assertEqual(
            {
                node.attrib["data-id"]
                for node in root.findall("svg:g[@class='node']", namespace)
            },
            {node["id"] for node in document["nodes"]},
        )
        self.assertEqual(
            {
                (
                    edge.attrib["data-source"],
                    edge.attrib["data-target"],
                    edge.attrib["data-kind"],
                    edge.attrib["data-label"],
                )
                for edge in root.findall("svg:g[@class='edge']", namespace)
            },
            {
                (edge["source"], edge["target"], edge["kind"], edge["label"])
                for edge in document["edges"]
            },
        )


if __name__ == "__main__":
    unittest.main()
