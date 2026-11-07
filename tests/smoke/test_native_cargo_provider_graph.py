"""Native Cargo graph values refuse alias ambiguity and invalid provider closure."""

import unittest
from copy import deepcopy
from dataclasses import replace

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.native_cargo_provider_graph import (
    MAX_PROVIDER_NODES,
    NativeCargoProviderGraphError,
)
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderEdge as Edge,
)
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderGraph as Graph,
)
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderNode as Node,
)


def identity(label):
    return canonical_identity({"native-cargo-provider-test": label})


def node(name):
    return Node(
        *(
            identity(name + suffix)
            for suffix in ("revision", "artifact", "surface", "source", "manifest")
        ),
        name,
        "1.0.0",
    )


def graph(root, nodes=(), edges=()):
    return Graph(
        root,
        tuple(sorted(nodes, key=lambda row: row.component_revision.uri)),
        tuple(
            sorted(
                edges,
                key=lambda row: (
                    row.consumer_revision.uri,
                    row.alias,
                    row.provider_revision.uri,
                ),
            )
        ),
    )


class NativeCargoProviderGraphTests(unittest.TestCase):
    def setUp(self):
        self.root = identity("consumer")
        self.alpha, self.beta = node("alpha"), node("beta")
        self.diamond = (
            Edge(self.root, self.alpha.component_revision, "alpha"),
            Edge(self.root, self.beta.component_revision, "beta"),
            Edge(self.alpha.component_revision, self.beta.component_revision, "beta"),
        )

    def test_diamond_has_one_nominal_provider_and_portable_identity(self):
        value = graph(self.root, (self.alpha, self.beta), self.diamond)
        self.assertEqual(len(value.nodes), 2)
        self.assertEqual(value.identity, canonical_identity(value.to_dict()))
        self.assertNotIn("path", str(value.to_dict()))
        self.assertNotEqual(
            value.identity,
            graph(self.root, (self.alpha, self.beta), self.diamond[:2]).identity,
        )

    def test_dependency_free_graph_remains_valid(self):
        self.assertEqual(graph(self.root).to_dict()["nodes"], [])

    def test_missing_provider_and_disconnected_node_refused(self):
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "missing_provider"):
            graph(self.root, (self.alpha,), self.diamond)
        with self.assertRaisesRegex(
            NativeCargoProviderGraphError, "unreachable_provider"
        ):
            graph(self.root, (self.alpha,))

    def test_direct_and_disconnected_cycles_refused(self):
        edges = self.diamond + (
            Edge(self.beta.component_revision, self.alpha.component_revision, "alpha"),
        )
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "cycle"):
            graph(self.root, (self.alpha, self.beta), edges)
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "cycle"):
            graph(
                self.root,
                (self.alpha,),
                (
                    Edge(
                        self.alpha.component_revision,
                        self.alpha.component_revision,
                        "alpha",
                    ),
                ),
            )

    def test_substitution_changes_graph_identity(self):
        original = graph(self.root, (self.alpha, self.beta), self.diamond)
        for field in (
            "artifact_identity",
            "import_surface_identity",
            "source_tree_identity",
            "original_manifest_identity",
        ):
            changed = replace(self.beta, **{field: identity("substituted")})
            self.assertNotEqual(
                original.identity,
                graph(self.root, (self.alpha, changed), self.diamond).identity,
            )

    def test_duplicate_package_normalization_and_alias_refused(self):
        for nodes in (
            (self.alpha, self.alpha),
            (self.alpha, replace(self.beta, package="alpha")),
            (
                replace(self.alpha, package="some-name"),
                replace(self.beta, package="some_name"),
            ),
        ):
            with self.assertRaisesRegex(
                NativeCargoProviderGraphError, "duplicate_provider"
            ):
                graph(self.root, nodes, self.diamond)
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "duplicate_alias"):
            graph(
                self.root,
                (self.alpha, self.beta),
                self.diamond
                + (Edge(self.root, self.beta.component_revision, "alpha"),),
            )
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "duplicate_alias"):
            graph(
                self.root,
                (self.alpha, self.beta),
                (
                    Edge(self.root, self.alpha.component_revision, "some-name"),
                    Edge(self.root, self.beta.component_revision, "some_name"),
                ),
            )

    def test_order_bounds_identity_and_package_input_refused(self):
        value = graph(self.root, (self.alpha, self.beta), self.diamond)
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "node_order"):
            Graph(value.root_revision, tuple(reversed(value.nodes)), value.edges)
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "edge_order"):
            Graph(value.root_revision, value.nodes, tuple(reversed(value.edges)))
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "nodes"):
            Graph(self.root, (self.alpha,) * (MAX_PROVIDER_NODES + 1), ())
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "identity"):
            Graph("not-an-identity", (), ())
        for field, invalid in (
            ("package", "../escape"),
            ("version", "1.0.0\n"),
            ("version", "01.0.0"),
            ("version", "1.0.0-01"),
        ):
            with self.assertRaises(NativeCargoProviderGraphError):
                replace(self.beta, **{field: invalid})

    def test_closed_wire_decode_retains_exact_graph_and_rejects_added_authority(self):
        value = graph(self.root, (self.alpha, self.beta), self.diamond)
        self.assertEqual(Graph.from_dict(value.to_dict()), value)
        changes = [
            lambda raw: raw.update({"local_path": "/ambient"}),
            lambda raw: raw.update({"schema": "future"}),
            lambda raw: raw["nodes"][0].update({"admitted": True}),
            lambda raw: raw["edges"][0].update({"path": "../escape"}),
            lambda raw: raw.update({"root_revision": "not-an-identity"}),
        ]
        for change in changes:
            raw = deepcopy(value.to_dict())
            change(raw)
            with self.assertRaises(NativeCargoProviderGraphError):
                Graph.from_dict(raw)
