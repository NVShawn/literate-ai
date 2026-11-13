"""Stable projection locations are data coordinates, never input admission."""

import json
import unittest
from dataclasses import replace

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderGraph,
    NativeCargoProviderGraphError,
)
from tests.smoke import test_native_cargo_projection as fixture


def expected_coordinate(node):
    # Authored independently of the projector and its execution-context export.
    return canonical_identity(
        {
            "schema": "literate-ai/native-cargo-provider-coordinate@1",
            **{
                key: value
                for key, value in node.to_dict().items()
                if key != "artifact_identity"
            },
        }
    ).digest


class NativeCargoCoordinateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NativeCargoProjectionTests(
            "test_clean_projections_equal_and_original_values_unchanged"
        )
        self.fixture.setUp()

    def project(self, replacement=None):
        fixture = self.fixture
        sources = tuple(
            replace(source, node=replacement)
            if replacement is not None
            and source.node.component_revision == replacement.component_revision
            else source
            for source in fixture.sources
        )
        graph = replace(
            fixture.graph,
            nodes=tuple(
                sorted(
                    (source.node for source in sources),
                    key=lambda node: node.component_revision.uri,
                )
            ),
        )
        return fixture.projection(graph=graph, sources=sources), graph

    @staticmethod
    def directories(projection):
        return {
            row.path.split("/")[3]
            for row in projection[1]
            if row.path.startswith(".literate/native-cargo/providers/")
        }

    def test_locations_match_owned_material_coordinate(self):
        projected, graph = self.project()
        self.assertEqual(
            self.directories(projected),
            {expected_coordinate(node) for node in graph.nodes},
        )

    def test_00_new_export_context_preserves_locations_and_changes_provenance(self):
        original, graph = self.project()
        changed, current = self.project(
            replace(
                self.fixture.beta.node, artifact_identity=fixture.identity("new-grant")
            )
        )
        self.assertEqual(self.directories(original), self.directories(changed))
        self.assertEqual(original[0], changed[0])
        records = [
            next(row for row in value[1] if row.path.endswith("projection.json"))
            for value in (original, changed)
        ]
        self.assertNotEqual(records[0].content, records[1].content)
        self.assertEqual(json.loads(records[1].content)["graph"], current.to_dict())
        self.assertNotEqual(graph.identity, current.identity)

    def test_changed_source_or_import_material_changes_coordinate(self):
        original, _ = self.project()
        for field in ("source_tree_identity", "import_surface_identity"):
            with self.subTest(field=field):
                changed, _ = self.project(
                    replace(
                        self.fixture.beta.node, **{field: fixture.identity("changed")}
                    )
                )
                self.assertNotEqual(
                    self.directories(original), self.directories(changed)
                )

    def test_same_revision_conflicting_material_refuses(self):
        graph = self.fixture.graph
        original = self.fixture.beta.node
        conflict = replace(
            original,
            artifact_identity=fixture.identity("conflicting-export"),
            source_tree_identity=fixture.identity("conflicting-source"),
            import_surface_identity=fixture.identity("conflicting-interface"),
            package="other_package",
        )
        with self.assertRaisesRegex(
            NativeCargoProviderGraphError, "duplicate_provider"
        ):
            NativeCargoProviderGraph(
                graph.root_revision,
                tuple(
                    sorted(
                        (*graph.nodes, conflict),
                        key=lambda node: node.component_revision.uri,
                    )
                ),
                graph.edges,
            )
