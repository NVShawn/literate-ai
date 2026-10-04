"""Accepted provider closure follows artifact dependencies, including diamonds."""

import unittest
from dataclasses import replace

from literate_ai.adapters.action_build_record import validate_build_provider_receipts
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.application.standard_provider_receipts import (
    select_build_provider_receipts,
)
from literate_ai.contracts import canonical_identity
from tests.support.fixtures_test_action_build_intent import provider_evidence
from tests.support.fixtures_test_standard_post_source_evidence import _multi_evidence


def receipt(name, dependencies=()):
    value = provider_evidence(canonical_identity(name))
    exports = tuple(
        replace(
            item,
            dependency_artifact_identities=tuple(
                sorted(
                    {
                        export.identity
                        for dependency in dependencies
                        for export in dependency.build.exports
                    },
                    key=lambda item: item.uri,
                )
            ),
        )
        for item in value.build.exports
    )
    build = replace(
        value.build,
        exports=exports,
        resolved_sbom_export_identities=tuple(item.identity for item in exports),
    )
    return replace(
        value,
        build=build,
        generated_tests=replace(
            value.generated_tests,
            build_evidence_identity=build.identity,
            export_identities=build.export_identities,
        ),
        execution=replace(
            value.execution,
            build_evidence_identity=build.identity,
            export_identities=build.export_identities,
            root_export_identity=exports[0].identity,
        ),
    )


class StandardProviderReceiptTests(unittest.TestCase):
    def setUp(self):
        self.leaf = receipt("leaf")
        self.left = receipt("left", (self.leaf,))
        self.right = receipt("right", (self.leaf,))
        self.roots = tuple(
            sorted(
                (*self.left.build.exports, *self.right.build.exports),
                key=lambda item: item.identity.uri,
            )
        )
        self.expected = tuple(
            sorted(
                (self.leaf, self.left, self.right),
                key=lambda item: item.component_revision.uri,
            )
        )

    def test_shared_dependency_is_selected_once_and_unrelated_inventory_is_omitted(
        self,
    ):
        inventory = (self.right, receipt("unrelated"), self.leaf, self.left)
        self.assertEqual(
            select_build_provider_receipts(self.roots, inventory), self.expected
        )
        validate_build_provider_receipts(self.roots, self.expected)
        self.assertEqual(select_build_provider_receipts((), inventory), ())

    def test_missing_transitive_receipt_and_unrelated_wire_receipt_refuse(self):
        with self.assertRaises(ValueError):
            select_build_provider_receipts(self.roots, (self.left, self.right))
        extra = tuple(
            sorted(
                (*self.expected, receipt("unrelated")),
                key=lambda item: item.component_revision.uri,
            )
        )
        with self.assertRaises(ActionWireError):
            validate_build_provider_receipts(self.roots, extra)

    def test_substituted_dependency_receipt_and_duplicate_inventory_refuse(self):
        changed = receipt("leaf", (receipt("foreign"),))
        for inventory in (
            (self.left, self.right, changed),
            (*self.expected, self.leaf),
        ):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                select_build_provider_receipts(self.roots, inventory)

    def test_incomplete_direct_export_set_refuses(self):
        multi = _multi_evidence()
        with self.assertRaises(ValueError):
            select_build_provider_receipts(multi.build.exports[:1], (multi,))

    def test_noncanonical_direct_exports_refuse(self):
        for providers in (self.roots[::-1], self.roots + self.roots[:1]):
            with self.subTest(providers=providers), self.assertRaises(ValueError):
                select_build_provider_receipts(providers, self.expected)
