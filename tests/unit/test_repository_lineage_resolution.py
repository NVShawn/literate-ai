from __future__ import annotations

import random
import unittest

from literate_ai.application.repository_lineage import (
    RepositoryLineageResolutionError,
    ResolvedRepositorySnapshot,
    resolve_repository_lineage,
)
from literate_ai.contracts import (
    ContentIdentity,
    HashAlgorithm,
    RepositoryParentReference,
    RepositoryParentSelection,
)


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def reference(name: str, revision: str = "main") -> RepositoryParentReference:
    return RepositoryParentReference(f"https://example.test/{name}.git", revision)


class FakeProvider:
    def __init__(self, snapshots: tuple[ResolvedRepositorySnapshot, ...]) -> None:
        self.snapshots = {item.reference.repository_url: item for item in snapshots}
        self.calls: list[RepositoryParentReference] = []

    def resolve(
        self, requested: RepositoryParentReference
    ) -> ResolvedRepositorySnapshot:
        self.calls.append(requested)
        return self.snapshots[requested.repository_url]


def snapshot(
    name: str,
    character: str,
    *parents: RepositoryParentReference,
) -> ResolvedRepositorySnapshot:
    item = reference(name)
    selection = (
        RepositoryParentSelection.inherit(tuple(parents))
        if parents
        else RepositoryParentSelection.root()
    )
    return ResolvedRepositorySnapshot(
        item,
        character * 40,
        name,
        identity(character),
        selection,
    )


class RepositoryLineageResolutionTests(unittest.TestCase):
    def test_resolves_a_diamond_to_every_root_in_ancestor_first_order(self) -> None:
        root = snapshot("root", "1")
        left = snapshot("left", "2", root.reference)
        right = snapshot("right", "3", root.reference)
        provider = FakeProvider((root, left, right))
        selection = RepositoryParentSelection.inherit((left.reference, right.reference))

        lineage = resolve_repository_lineage(selection, provider)

        self.assertEqual(
            tuple(item.project_id for item in lineage.nodes),
            ("root", "left", "right"),
        )
        self.assertEqual(provider.calls.count(root.reference), 1)
        self.assertEqual(
            set(lineage.selected_parents),
            {lineage.nodes[1].identity, lineage.nodes[2].identity},
        )

    def test_multi_parent_deep_dag_is_independent_of_selection_and_parent_order(
        self,
    ) -> None:
        root = snapshot("root", "1")
        left = snapshot("left", "2", root.reference)
        right = snapshot("right", "3", root.reference)
        join = snapshot("join", "4", left.reference, right.reference)
        leaf = snapshot("leaf", "5", join.reference)
        snapshots = (root, left, right, join, leaf)
        expected = resolve_repository_lineage(
            RepositoryParentSelection.inherit((leaf.reference, right.reference)),
            FakeProvider(snapshots),
        )
        randomizer = random.Random(11)
        for _ in range(24):
            shuffled = list(snapshots)
            randomizer.shuffle(shuffled)
            observed = resolve_repository_lineage(
                RepositoryParentSelection.inherit(
                    tuple(reversed((leaf.reference, right.reference)))
                ),
                FakeProvider(tuple(shuffled)),
            )
            self.assertEqual(
                tuple(node.project_id for node in observed.nodes),
                ("root", "left", "right", "join", "leaf"),
            )
            self.assertEqual(observed.nodes, expected.nodes)
            self.assertEqual(
                set(observed.selected_parents), set(expected.selected_parents)
            )

    def test_declared_grandparent_is_always_resolved(self) -> None:
        root = snapshot("root", "1")
        parent = snapshot("parent", "2", root.reference)
        provider = FakeProvider((root, parent))

        lineage = resolve_repository_lineage(
            RepositoryParentSelection.inherit((parent.reference,)), provider
        )

        self.assertEqual(
            tuple(item.project_id for item in lineage.nodes),
            ("root", "parent"),
        )
        self.assertEqual(lineage.nodes[0].project_id, "root")

    def test_cycle_fails_before_a_lineage_can_be_materialized(self) -> None:
        left_ref = reference("left")
        right_ref = reference("right")
        left = ResolvedRepositorySnapshot(
            left_ref,
            "1" * 40,
            "left",
            identity("1"),
            RepositoryParentSelection.inherit((right_ref,)),
        )
        right = ResolvedRepositorySnapshot(
            right_ref,
            "2" * 40,
            "right",
            identity("2"),
            RepositoryParentSelection.inherit((left_ref,)),
        )

        with self.assertRaisesRegex(
            RepositoryLineageResolutionError, "cycle"
        ) as raised:
            resolve_repository_lineage(
                RepositoryParentSelection.inherit((left_ref,)),
                FakeProvider((left, right)),
            )
        self.assertEqual(raised.exception.code, "repository_lineage.cycle")
        self.assertEqual(
            raised.exception.message,
            "repository parent declarations contain a cycle: "
            "repository:left -> repository:right -> repository:left",
        )

    def test_shortest_cycle_is_reported_deterministically(self) -> None:
        alpha_ref = reference("alpha")
        beta_ref = reference("beta")
        gamma_ref = reference("gamma")
        delta_ref = reference("delta")
        alpha = ResolvedRepositorySnapshot(
            alpha_ref,
            "1" * 40,
            "alpha",
            identity("1"),
            RepositoryParentSelection.inherit((beta_ref, delta_ref)),
        )
        beta = ResolvedRepositorySnapshot(
            beta_ref,
            "2" * 40,
            "beta",
            identity("2"),
            RepositoryParentSelection.inherit((gamma_ref,)),
        )
        gamma = ResolvedRepositorySnapshot(
            gamma_ref,
            "3" * 40,
            "gamma",
            identity("3"),
            RepositoryParentSelection.inherit((alpha_ref,)),
        )
        delta = ResolvedRepositorySnapshot(
            delta_ref,
            "4" * 40,
            "delta",
            identity("4"),
            RepositoryParentSelection.inherit((alpha_ref,)),
        )

        with self.assertRaises(RepositoryLineageResolutionError) as raised:
            resolve_repository_lineage(
                RepositoryParentSelection.inherit((alpha_ref,)),
                FakeProvider((alpha, beta, gamma, delta)),
            )

        self.assertEqual(raised.exception.code, "repository_lineage.cycle")
        self.assertEqual(
            raised.exception.message,
            "repository parent declarations contain a cycle: "
            "repository:alpha -> repository:delta -> repository:alpha",
        )

    def test_one_url_at_two_revisions_fails_closed(self) -> None:
        common_main = reference("common", "main")
        common_release = reference("common", "release")
        left = snapshot("left", "2", common_main)
        right = snapshot("right", "3", common_release)
        common = snapshot("common", "1")
        provider = FakeProvider((common, left, right))

        with self.assertRaises(RepositoryLineageResolutionError) as raised:
            resolve_repository_lineage(
                RepositoryParentSelection.inherit((left.reference, right.reference)),
                provider,
            )
        self.assertEqual(
            raised.exception.code, "repository_lineage.conflicting_revision"
        )

    def test_provider_cannot_substitute_a_different_reference(self) -> None:
        requested = reference("requested")
        substituted = snapshot("substituted", "1")
        provider = FakeProvider((substituted,))
        provider.snapshots[requested.repository_url] = substituted

        with self.assertRaises(RepositoryLineageResolutionError) as raised:
            resolve_repository_lineage(
                RepositoryParentSelection.inherit((requested,)), provider
            )
        self.assertEqual(raised.exception.code, "repository_lineage.snapshot_mismatch")


if __name__ == "__main__":
    unittest.main()
