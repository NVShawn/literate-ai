"""Exact native lock/SBOM joins; these do not qualify Standard invocation."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.dependencies.acquisition import _generated_lock_projection
from literate_ai.adapters.dependencies.native_cargo_lock import (
    require_native_cargo_managed_graph,
)
from literate_ai.adapters.dependencies.resolution import _resolved_source_projection
from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.contracts import (
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    component_bom_ref,
)
from tests.smoke import test_native_cargo_projection as fixture


class NativeCargoLockTests(unittest.TestCase):
    def setUp(self):
        f = fixture.NativeCargoProjectionTests()
        f.setUp()
        self.graph, self.manifest = f.graph, f.original.decode()
        self.root_ref = component_bom_ref(f.root)
        self.alpha_ref = component_bom_ref(f.alpha.node.component_revision)
        self.beta_ref = component_bom_ref(f.beta.node.component_revision)
        nodes = [
            CycloneDxManagedComponent(
                self.root_ref,
                ManagedComponentKind.ROOT,
                f.root,
                "consumer",
                "9.0.0",
                (),
            ),
            *(
                CycloneDxManagedComponent(
                    component_bom_ref(node.component_revision),
                    ManagedComponentKind.COMPONENT,
                    node.component_revision,
                    node.package,
                    "8.0.0",
                    ("runtime",),
                )
                for node in self.graph.nodes
            ),
        ]
        self.managed = CycloneDxManagedGraph(
            self.root_ref,
            tuple(sorted(nodes, key=lambda row: row.bom_ref)),
            tuple(
                sorted(
                    CycloneDxManagedEdge(
                        component_bom_ref(edge.consumer_revision),
                        component_bom_ref(edge.provider_revision),
                    )
                    for edge in self.graph.edges
                )
            ),
            fixture.identity("resolved"),
        )
        self.lock = """version = 4
[[package]]
name = "consumer"
version = "1.0.0"
dependencies = ["alpha", "beta"]
[[package]]
name = "alpha"
version = "1.0.0"
dependencies = ["beta"]
[[package]]
name = "beta"
version = "1.0.0"
"""

    def project(self, lock=None, **changes):
        args = dict(
            native_cargo_graph=self.graph, native_cargo_manifest="source/Cargo.toml"
        )
        args.update(changes)
        return _generated_lock_projection(
            {
                "source/Cargo.toml": self.manifest,
                "source/Cargo.lock": lock or self.lock,
            },
            **args,
        )

    def resolve(self, projection, graph=None, managed=None):
        managed = managed or self.managed
        content, _ = build_cyclonedx_bom(
            managed_graph=managed, lifecycle=CycloneDxLifecycle.SOURCE
        )
        return _resolved_source_projection(
            content,
            managed_graph=managed,
            resolution_candidates=(),
            lock_projection=projection,
            native_cargo_graph=graph or self.graph,
        )

    def test_shared_native_inventory_maps_to_managed_refs_without_fabricated_packages(
        self,
    ):
        projection = self.project()
        self.assertEqual(
            {row.managed_ref for row in projection.packages},
            {self.root_ref, self.alpha_ref, self.beta_ref},
        )
        components, edges = self.resolve(projection)
        self.assertEqual(components, ())
        self.assertEqual(
            set(edges),
            {
                (self.root_ref, self.alpha_ref),
                (self.root_ref, self.beta_ref),
                (self.alpha_ref, self.beta_ref),
            },
        )
        # Component versions are authored identities, not Cargo package versions.
        source, _ = build_cyclonedx_bom(
            managed_graph=self.managed,
            lifecycle=CycloneDxLifecycle.SOURCE,
        )
        resolved, _ = build_cyclonedx_bom(
            managed_graph=self.managed,
            lifecycle=CycloneDxLifecycle.RESOLVED,
            source_bom=source,
            additional_components=components,
            additional_edges=edges,
        )
        document = json.loads(resolved)
        inventory = [document["metadata"]["component"], *document["components"]]
        self.assertEqual(
            {row["bom-ref"]: row["version"] for row in inventory},
            {self.root_ref: "9.0.0", self.alpha_ref: "8.0.0", self.beta_ref: "8.0.0"},
        )

    def test_default_profile_and_wrong_manifest_selector_still_refuse(self):
        with self.assertRaises(DependencyObservationError):
            self.project(native_cargo_graph=None, native_cargo_manifest=None)
        for changes in (
            {"native_cargo_manifest": "other/Cargo.toml"},
            {"native_cargo_graph": None},
            {"native_cargo_graph": self.graph.to_dict()},
        ):
            with self.assertRaises(DependencyObservationError):
                self.project(**changes)

    def test_missing_duplicate_extra_hashed_or_registry_native_packages_refuse(self):
        changes = [
            self.lock.replace(
                'name = "beta"\nversion = "1.0.0"', 'name = "beta"\nversion = "2.0.0"'
            ),
            self.lock + '[[package]]\nname="beta"\nversion="1.0.0"\n',
            self.lock + '[[package]]\nname="other"\nversion="1.0.0"\n',
            self.lock + 'checksum="' + "a" * 64 + '"\n',
            self.lock + 'source="registry+https://example.invalid/index"\n',
            self.lock + '[[package]]\nname="Beta"\nversion="2.0.0"\n'
            'source="registry+https://example.invalid/index"\n',
        ]
        for lock in changes:
            with self.subTest(lock=lock), self.assertRaises(DependencyObservationError):
                self.project(lock)

    def test_native_edges_and_dependency_selectors_must_match_exactly(self):
        for lock in (
            self.lock.replace(
                'dependencies = ["alpha", "beta"]', 'dependencies = ["alpha"]'
            ),
            self.lock.replace('dependencies = ["beta"]', "dependencies = []"),
            self.lock.replace(
                'dependencies = ["beta"]', 'dependencies = ["alpha", "beta"]'
            ),
            self.lock.replace(
                'dependencies = ["beta"]', 'dependencies = ["beta", "beta"]'
            ),
            self.lock.replace(
                'dependencies = ["beta"]', 'dependencies = ["beta 2.0.0"]'
            ),
            self.lock.replace(
                'dependencies = ["beta"]', 'dependencies = ["beta 1.0.0 (wrong)"]'
            ),
        ):
            with self.subTest(lock=lock), self.assertRaises(DependencyObservationError):
                self.project(lock)

    def test_registry_packages_keep_real_checksums_and_cannot_depend_on_native_nodes(
        self,
    ):
        lock = self.lock.replace(
            'dependencies = ["beta"]', 'dependencies = ["beta", "external"]'
        )
        lock += '[[package]]\nname="external"\nversion="2.0.0"\n'
        registry = (
            'source="registry+https://example.invalid/index"\nchecksum="'
            + "a" * 64
            + '"\n'
        )
        projection = self.project(lock + registry)
        external = next(
            row for row in projection.packages if row.coordinate == "external"
        )
        self.assertIsNone(external.managed_ref)
        self.assertEqual(external.integrity_digest, "a" * 64)
        components, edges = self.resolve(projection)
        self.assertEqual(
            [row["bom-ref"] for row in components], ["pkg:cargo/external@2.0.0"]
        )
        self.assertIn((self.alpha_ref, "pkg:cargo/external@2.0.0"), edges)
        for invalid in (lock, lock + registry + 'dependencies=["beta"]\n'):
            with self.assertRaises(DependencyObservationError):
                self.project(invalid)

    def test_managed_graph_root_inventory_edges_and_unbound_refs_refuse(self):
        without_alpha = replace(
            self.managed,
            edges=tuple(
                row for row in self.managed.edges if row.source_ref != self.alpha_ref
            ),
        )
        with self.assertRaises(DependencyObservationError):
            require_native_cargo_managed_graph(self.graph, without_alpha)
        other = replace(
            self.graph,
            root_revision=fixture.identity("other"),
            edges=tuple(
                sorted(
                    (
                        replace(edge, consumer_revision=fixture.identity("other"))
                        if edge.consumer_revision == self.graph.root_revision
                        else edge
                        for edge in self.graph.edges
                    ),
                    key=lambda edge: (
                        edge.consumer_revision.uri,
                        edge.alias,
                        edge.provider_revision.uri,
                    ),
                )
            ),
        )
        with self.assertRaises(DependencyObservationError):
            require_native_cargo_managed_graph(other, self.managed)
        projection = self.project()
        changed = replace(
            projection,
            packages=tuple(
                replace(row, managed_ref=self.alpha_ref)
                if row.coordinate == "beta"
                else row
                for row in projection.packages
            ),
        )
        with self.assertRaises(DependencyObservationError):
            self.resolve(changed)
        with self.assertRaises(DependencyObservationError):
            _resolved_source_projection(
                json.dumps({"components": [], "dependencies": []}).encode(),
                managed_graph=self.managed,
                resolution_candidates=(),
                lock_projection=projection,
            )
