"""Shared fixtures extracted from ``tests.unit.test_retained_library_exports``."""

import copy

import unittest

from dataclasses import replace

from literate_ai.application.artifact_graph import create_artifact_build_graph

from literate_ai.contracts import ContractValidationError, canonical_identity

from literate_ai.contracts.library_products import LibraryArtifactProduct

from literate_ai.contracts.retained_libraries import RetainedLibraryExportSet

from tests.support import fixtures_test_artifact_graph_contracts as graph_fixtures

from tests.support import fixtures_test_library_products as library_fixtures

from tests.support import fixtures_test_schema_catalog as schema_fixtures

class RetainedLibraryExportTests(unittest.TestCase):
    def fixture(self):
        fixture = graph_fixtures.ArtifactGraphTests()
        native = fixture.export("native")
        dependency = replace(fixture.export("dependency", (native,)), role="library")
        root = replace(fixture.export("root", (dependency, native)), role="library")
        graph = create_artifact_build_graph(
            build_system_driver_identity=fixture.driver,
            manifests=(
                fixture.manifest(native),
                fixture.manifest(dependency, (native,)),
                fixture.manifest(root, (dependency, native)),
            ),
            link_roots=(root.identity,),
        )
        products = tuple(
            sorted(
                (
                    LibraryArtifactProduct(
                        export,
                        replace(
                            library_fixtures.library_product("rust").import_surface,
                            package=export.export_id,
                        ),
                    )
                    for export in (dependency, root)
                ),
                key=lambda item: item.artifact_export.identity.uri,
            )
        )
        return RetainedLibraryExportSet(graph, graph.link_plans[0].identity, products)

    def test_round_trip_preserves_transitive_native_dependencies_and_public_shape(self):
        value = self.fixture()
        self.assertEqual(len(value.link_plan.ordered_artifact_identities), 3)
        self.assertEqual(len(value.libraries), 2)
        self.assertEqual(RetainedLibraryExportSet.from_dict(value.to_dict()), value)
        schema_fixtures.SchemaCatalog().validate(value.SCHEMA, value.to_dict())

    def test_grouped_library_roots_keep_one_exact_shared_dependency_closure(self):
        value = self.fixture()
        graph = create_artifact_build_graph(
            build_system_driver_identity=value.graph.build_system_driver_identity,
            manifests=value.graph.manifests,
            link_roots=(),
            link_root_groups=(
                tuple(item.artifact_export.identity for item in value.libraries),
            ),
        )
        grouped = replace(
            value, graph=graph, link_plan_identity=graph.link_plans[0].identity
        )
        self.assertEqual(len(grouped.link_plan.resolved_root_artifact_identities), 2)
        self.assertEqual(len(grouped.link_plan.ordered_artifact_identities), 3)
        self.assertEqual(RetainedLibraryExportSet.from_dict(grouped.to_dict()), grouped)

    def test_absent_duplicate_or_substituted_library_products_refuse(self):
        value = self.fixture()
        wrong = replace(
            value.libraries[0],
            artifact_export=replace(
                value.libraries[0].artifact_export,
                target_identity=canonical_identity("other target"),
            ),
        )
        for products in (
            (),
            value.libraries[:1],
            value.libraries[::-1],
            value.libraries + value.libraries[:1],
            (wrong, value.libraries[1]),
        ):
            with self.subTest(products=products):
                with self.assertRaises(ContractValidationError):
                    replace(value, libraries=products)

    def test_missing_link_and_non_library_root_refuse(self):
        value = self.fixture()
        with self.assertRaises(ContractValidationError):
            replace(value, link_plan_identity=canonical_identity("missing"))
        native = next(
            item
            for manifest in value.graph.manifests
            for item in manifest.exports
            if item.role != "library"
        )
        graph = create_artifact_build_graph(
            build_system_driver_identity=value.graph.build_system_driver_identity,
            manifests=value.graph.manifests,
            link_roots=(native.identity,),
        )
        with self.assertRaises(ContractValidationError):
            replace(value, graph=graph, link_plan_identity=graph.link_plans[0].identity)

    def test_import_interface_changes_bind_identity_but_never_establish_qualification(
        self,
    ):
        value = self.fixture()
        product = value.libraries[0]
        capability = product.import_surface.capabilities[0]
        changed = replace(
            product,
            import_surface=replace(
                product.import_surface,
                capabilities=(
                    replace(
                        capability,
                        interface_identity=canonical_identity("different interface"),
                    ),
                ),
            ),
        )
        other = replace(value, libraries=(changed, *value.libraries[1:]))
        self.assertNotEqual(other.identity, value.identity)
        # Structural metadata may describe a different interface. Only independent
        # qualification admission can authorize either record for consumption.
        for key in ("qualified", "accepted", "authenticated", "trust"):
            wire = value.to_dict() | {key: True}
            with self.subTest(key=key), self.assertRaises(ContractValidationError):
                RetainedLibraryExportSet.from_dict(wire)

    def test_truncated_graph_refuses_even_when_products_remain(self):
        value = self.fixture()
        for index in range(len(value.graph.manifests)):
            wire = copy.deepcopy(value.to_dict())
            del wire["graph"]["manifests"][index]
            with (
                self.subTest(removed=index),
                self.assertRaises(ContractValidationError),
            ):
                RetainedLibraryExportSet.from_dict(wire)

    def test_untyped_or_oversized_products_refuse_before_parsing(self):
        value = self.fixture()
        for products in ([value.libraries[0]] * 1025, [None] * 1025):
            wire = value.to_dict() | {"libraries": products}
            with self.assertRaises(ContractValidationError):
                RetainedLibraryExportSet.from_dict(wire)
        with self.assertRaises(ContractValidationError):
            replace(value, libraries=list(value.libraries))

