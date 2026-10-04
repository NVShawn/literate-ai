from __future__ import annotations

import unittest

from literate_ai.contracts import (
    ContentIdentity,
    ContractValidationError,
    HashAlgorithm,
    RepositoryLineage,
    RepositoryLineageNode,
    RepositoryParentMode,
    RepositoryParentReference,
    RepositoryParentSelection,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def reference(name: str, revision: str = "main") -> RepositoryParentReference:
    return RepositoryParentReference(f"https://example.test/{name}.git", revision)


def fixture() -> tuple[
    RepositoryParentSelection,
    RepositoryLineageNode,
    RepositoryLineageNode,
    RepositoryLineage,
]:
    root_ref = reference("root")
    child_ref = reference("child", "release")
    root = RepositoryLineageNode(
        "root",
        identity("1"),
        root_ref.repository_url,
        root_ref.requested_revision,
        "a" * 40,
        RepositoryParentSelection.root(),
        (),
    )
    child = RepositoryLineageNode(
        "child",
        identity("2"),
        child_ref.repository_url,
        child_ref.requested_revision,
        "b" * 40,
        RepositoryParentSelection.inherit((root_ref,)),
        (root.identity,),
    )
    selection = RepositoryParentSelection.inherit((child_ref,))
    return (
        selection,
        root,
        child,
        RepositoryLineage(selection, (root, child), (child.identity,)),
    )


class RepositoryLineageContractTests(unittest.TestCase):
    def test_explicit_root_round_trips_without_nodes(self) -> None:
        selection = RepositoryParentSelection.root()
        lineage = RepositoryLineage(selection, (), ())

        self.assertEqual(selection.mode, RepositoryParentMode.ROOT)
        self.assertEqual(
            RepositoryParentSelection.from_dict(selection.to_dict()), selection
        )
        self.assertEqual(RepositoryLineage.from_dict(lineage.to_dict()), lineage)
        self.assertEqual(
            lineage.identity, RepositoryLineage.from_dict(lineage.to_dict()).identity
        )

    def test_three_level_selection_is_canonical_and_round_trips(self) -> None:
        selection, root, child, lineage = fixture()

        self.assertEqual(lineage.nodes, (root, child))
        self.assertEqual(lineage.selected_parents, (child.identity,))
        self.assertEqual(RepositoryLineage.from_dict(lineage.to_dict()), lineage)
        self.assertEqual(selection.identity, lineage.selection.identity)

    def test_declared_parent_cannot_be_truncated_from_lineage(self) -> None:
        selection, _root, child, _lineage = fixture()

        with self.assertRaisesRegex(ContractValidationError, "earlier ancestor nodes"):
            RepositoryLineage(selection, (child,), (child.identity,))

    def test_unreachable_node_is_rejected(self) -> None:
        selection, root, child, _lineage = fixture()
        unrelated_ref = reference("unrelated")
        unrelated = RepositoryLineageNode(
            "unrelated",
            identity("3"),
            unrelated_ref.repository_url,
            unrelated_ref.requested_revision,
            "c" * 40,
            RepositoryParentSelection.root(),
            (),
        )

        with self.assertRaisesRegex(ContractValidationError, "unreachable"):
            RepositoryLineage(selection, (root, unrelated, child), (child.identity,))

    def test_one_url_cannot_resolve_at_conflicting_revisions(self) -> None:
        selection, root, child, _lineage = fixture()
        duplicate = RepositoryLineageNode(
            "duplicate",
            identity("4"),
            root.repository_url,
            "other",
            "d" * 40,
            RepositoryParentSelection.root(),
            (),
        )

        with self.assertRaisesRegex(ContractValidationError, "repository URL"):
            RepositoryLineage(
                selection,
                (root, duplicate, child),
                (child.identity,),
            )

    def test_root_and_inherit_modes_have_exact_cardinality(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "empty in root mode"):
            RepositoryParentSelection(RepositoryParentMode.ROOT, (reference("root"),))
        with self.assertRaisesRegex(
            ContractValidationError, "nonempty in inherit mode"
        ):
            RepositoryParentSelection(RepositoryParentMode.INHERIT, ())

    def test_parent_urls_and_revision_selectors_reject_credential_and_option_inputs(
        self,
    ) -> None:
        with self.assertRaisesRegex(ContractValidationError, "credentials"):
            RepositoryParentReference(
                "https://user:secret@example.test/parent.git", "main"
            )
        with self.assertRaisesRegex(ContractValidationError, "safe Git revision"):
            RepositoryParentReference(
                "https://example.test/parent.git", "--upload-pack"
            )

    def test_wire_contracts_are_closed(self) -> None:
        selection, _root, _child, lineage = fixture()
        for contract in (selection.parents[0], selection, *lineage.nodes, lineage):
            value = contract.to_dict()
            value["implicit"] = True
            with self.subTest(contract=type(contract).__name__):
                with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
                    type(contract).from_dict(value)

    def test_wire_contracts_match_the_public_schema_catalog(self) -> None:
        selection, _root, _child, lineage = fixture()
        schemas = SchemaCatalog()
        for contract in (selection.parents[0], selection, *lineage.nodes, lineage):
            with self.subTest(contract=type(contract).__name__):
                schemas.validate(contract.SCHEMA, contract.to_dict())


if __name__ == "__main__":
    unittest.main()
