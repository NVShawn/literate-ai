from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from literate_ai.adapters.authority import (
    AuthorityProjectionStoreError,
    FileAuthorityProjectionStore,
)
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts import (
    ComponentAuthorityProjection,
    ComponentAuthorityState,
    ComponentAuthorityTransition,
    ContentIdentity,
)


def identity(character: str) -> ContentIdentity:
    return ContentIdentity.parse_uri(f"sha256:{character * 64}")


class ComponentAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = identity("1")
        self.provenance = identity("2")
        self.revision = identity("3")
        self.specification = identity("4")
        self.inventory = ComponentAuthorityLifecycle.inventory(
            component_coordinate="component://example/app",
            source_snapshot_identity=self.source,
            provenance_reference_identity=self.provenance,
            evidence_identities=(self.source,),
        )
        self.derived = ComponentAuthorityLifecycle.derive(
            self.inventory,
            component_revision_identity=self.revision,
            specification_set_identity=self.specification,
            evidence_identities=(self.specification,),
        )

    def test_store_rejects_skipped_transition_and_changed_carried_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileAuthorityProjectionStore(Path(temporary))
            store.append(self.inventory)
            changed_source = ComponentAuthorityProjection(
                component_coordinate=self.inventory.component_coordinate,
                component_revision_identity=self.revision,
                state=ComponentAuthorityState.SPEC_ASSISTED,
                transition=ComponentAuthorityTransition.SPEC_DERIVATION,
                source_snapshot_identity=identity("9"),
                specification_set_identity=self.specification,
                target_lock_identity=None,
                generation_closure=None,
                verifier_identity=None,
                policy_identity=None,
                evidence_identities=(self.specification,),
                provenance_reference_identity=self.provenance,
                prior_projection_identity=self.inventory.identity,
            )
            with self.assertRaises(AuthorityProjectionStoreError):
                store.append(changed_source)

            skipped = ComponentAuthorityProjection(
                component_coordinate=self.inventory.component_coordinate,
                component_revision_identity=self.revision,
                state=ComponentAuthorityState.DERIVED_SOURCE_RETAINED,
                transition=ComponentAuthorityTransition.HUMAN_ACCEPTANCE,
                source_snapshot_identity=identity("9"),
                specification_set_identity=self.specification,
                target_lock_identity=None,
                generation_closure=None,
                verifier_identity=None,
                policy_identity=None,
                evidence_identities=(identity("5"),),
                provenance_reference_identity=identity("8"),
                prior_projection_identity=self.inventory.identity,
            )
            with self.assertRaises(AuthorityProjectionStoreError):
                store.append(skipped)

    def test_store_rejects_symlinked_authority_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            try:
                (project / "provenance").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"host cannot create symlinks: {error}")
            with self.assertRaises(AuthorityProjectionStoreError):
                FileAuthorityProjectionStore(project).append(self.inventory)
            self.assertFalse((outside / "component-authority").exists())

    def test_concurrent_children_cannot_fork_current_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileAuthorityProjectionStore(Path(temporary))
            store.append(self.inventory)
            first = self.derived
            second = ComponentAuthorityLifecycle.derive(
                self.inventory,
                component_revision_identity=self.revision,
                specification_set_identity=self.specification,
                evidence_identities=(identity("e"),),
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(
                    executor.map(
                        lambda projection: self._append_outcome(store, projection),
                        (first, second),
                    )
                )
            self.assertEqual(sorted(outcomes), ["accepted", "rejected"])
            self.assertEqual(len(store.history("component://example/app")), 2)

    @staticmethod
    def _append_outcome(
        store: FileAuthorityProjectionStore,
        projection: ComponentAuthorityProjection,
    ) -> str:
        try:
            store.append(projection)
        except AuthorityProjectionStoreError:
            return "rejected"
        return "accepted"


if __name__ == "__main__":
    unittest.main()
