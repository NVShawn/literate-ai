"""Retained SBOM authority stays distinct from generated Component provenance."""

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.dependencies import (
    CycloneDxBomError,
    build_cyclonedx_bom,
    validate_cyclonedx_bom,
)
from literate_ai.contracts import CycloneDxLifecycle, canonical_identity
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectPlan,
    RetainedProjectProfile,
)
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.sbom import (
    CycloneDxManagedGraph,
    ManagedComponentKind,
    project_component_lock_managed_graph,
)
from tests.support.fixtures_test_component_lock_contracts import component_lock
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class RetainedProjectSbomTests(unittest.TestCase):
    def setUp(self):
        lock = component_lock()
        dependency = next(
            n for n in lock.nodes if n.revision.identity != lock.root_revision
        )
        second = replace(
            lock,
            root_revision=dependency.revision.identity,
            nodes=(dependency,),
            edges=(),
            _authorings=(dependency.revision.definition,),
        )
        self.locks = (lock, second)
        profile = RetainedProjectProfile(
            "host",
            *(canonical_identity(n) for n in range(5)),
            (("tool", "sdk/tool", canonical_identity("tool")),),
            (
                RetainedProjectAction(
                    "build", "build", "tool", (), (), ("app",), ("app",), 10
                ),
                RetainedProjectAction(
                    "test", "test", "tool", (), ("build",), ("case",), (), 10
                ),
                RetainedProjectAction(
                    "docs", "docs", "tool", (), ("build",), ("manual",), (), 10
                ),
                RetainedProjectAction(
                    "accept",
                    "accept",
                    "tool",
                    (),
                    ("test", "docs"),
                    ("verifier",),
                    (),
                    10,
                ),
            ),
            ("app",),
        )
        self.plan = RetainedProjectPlan(
            "original-project",
            canonical_identity("project authority"),
            tuple(item.identity for item in self.locks),
            canonical_identity("original conversion"),
            canonical_identity("standard binding"),
            canonical_identity("original binary manifest"),
            profile,
        )

    def graph(self):
        return project_retained_project_managed_graph(
            self.plan, self.locks, version="1.0.0"
        )

    def test_complete_locks_merge_shared_components_and_preserve_edge_authority(self):
        graph = self.graph()
        self.assertEqual(graph.resolved_graph_identity, self.plan.identity)
        self.assertEqual(CycloneDxManagedGraph.from_dict(graph.to_dict()), graph)
        SchemaCatalog().validate(graph.SCHEMA, graph.to_dict())
        root = next(c for c in graph.components if c.bom_ref == graph.root_ref)
        self.assertEqual(root.kind, ManagedComponentKind.RETAINED_PROJECT)
        self.assertEqual(root.binding_property, "literate-ai:retained-project")
        self.assertTrue(root.bom_ref.startswith("urn:literate-ai:retained-project:"))
        self.assertEqual(root.identity, self.plan.identity)
        self.assertEqual(len(graph.components), 3)
        original = project_component_lock_managed_graph(
            self.locks[0], self.locks[0].root_revision
        )
        self.assertTrue(set(original.edges) <= set(graph.edges))
        shared = next(
            c for c in graph.components if set(c.scopes) == {"build", "generation"}
        )
        self.assertEqual(shared.identity, self.locks[1].root_revision)

    def test_missing_duplicated_or_reordered_locks_refuse(self):
        for locks in (self.locks[:1], self.locks[::-1], (self.locks[0], self.locks[0])):
            with (
                self.subTest(locks=locks),
                self.assertRaisesRegex(ValueError, "lock-closure-mismatch"),
            ):
                project_retained_project_managed_graph(
                    self.plan, locks, version="1.0.0"
                )

    def test_conflicting_overlap_metadata_cannot_silently_overwrite_authority(self):
        graphs = tuple(
            project_component_lock_managed_graph(lock, lock.root_revision)
            for lock in self.locks
        )
        altered = replace(
            graphs[1],
            components=tuple(replace(c, version="2.0.0") for c in graphs[1].components),
        )
        with (
            patch(
                "literate_ai.contracts.retained_project_sbom.project_component_lock_managed_graph",
                side_effect=(graphs[0], altered),
            ),
            self.assertRaisesRegex(ValueError, "conflicting-managed-component"),
        ):
            self.graph()

    def test_review_changes_invalidate_managed_authority(self):
        changed = replace(
            self.plan, manifest_identity=canonical_identity("changed asset")
        )
        actual = project_retained_project_managed_graph(
            changed, self.locks, version="1.0.0"
        )
        self.assertNotEqual(self.graph().identity, actual.identity)
        self.assertNotEqual(self.graph().root_ref, actual.root_ref)

    def test_existing_bom_source_transition_retains_original_origin(self):
        graph = self.graph()
        source, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=graph
        )
        resolved, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=graph,
            source_bom=source,
        )
        validate_cyclonedx_bom(
            resolved,
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=graph,
            source_content=source,
        )
        root = json.loads(source)["metadata"]["component"]
        self.assertIn(
            {"name": "literate-ai:retained-project", "value": self.plan.identity.uri},
            root["properties"],
        )
        self.assertNotIn(
            "literate-ai:component-revision", {p["name"] for p in root["properties"]}
        )
        ordinary = project_component_lock_managed_graph(
            self.locks[0], self.locks[0].root_revision
        )
        with self.assertRaises(CycloneDxBomError):
            validate_cyclonedx_bom(
                source, lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=ordinary
            )
        self.assertEqual(binding.resolved_graph_identity, self.plan.identity)

    def test_unmanaged_dependency_cannot_claim_retained_managed_identity(self):
        graph = self.graph()
        for ref in (
            "pkg:generic/forged",
            "urn:literate-ai:retained-project:" + "a" * 64,
        ):
            component = {
                "type": "library",
                "bom-ref": ref,
                "name": "forged",
                "version": "1",
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "package"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    {
                        "name": "literate-ai:retained-project",
                        "value": self.plan.identity.uri,
                    },
                ],
            }
            with self.subTest(ref=ref), self.assertRaises(CycloneDxBomError):
                build_cyclonedx_bom(
                    lifecycle=CycloneDxLifecycle.SOURCE,
                    managed_graph=graph,
                    additional_components=(component,),
                    additional_edges=((graph.root_ref, ref),),
                )

    def test_retained_root_cannot_be_reclassified_as_a_component(self):
        root = next(c for c in self.graph().components if c.kind.is_root)
        with self.assertRaises(ValueError):
            replace(root, kind=ManagedComponentKind.ROOT)
        with self.assertRaises(ValueError):
            replace(root, scopes=("build",))
