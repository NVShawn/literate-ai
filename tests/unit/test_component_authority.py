from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.authority import (
    AuthorityProjectionStoreError,
    FileAuthorityProjectionStore,
)
from literate_ai.authority import AuthorityTransitionError, ComponentAuthorityLifecycle
from literate_ai.contracts import (
    ComponentAuthorityProjection,
    ComponentAuthorityState,
    ComponentAuthorityTransition,
    ComponentGenerationClosure,
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
        self.accepted = ComponentAuthorityLifecycle.accept(
            self.derived, evidence_identities=(identity("5"),)
        )

    def test_projection_round_trips_with_explicit_nulls(self) -> None:
        wire = self.inventory.to_dict()
        self.assertIsNone(wire["verifier_identity"])
        self.assertIsNone(wire["policy_identity"])
        self.assertEqual(ComponentAuthorityProjection.from_dict(wire), self.inventory)

    def test_direct_construction_enforces_schema_types_and_coordinate(self) -> None:
        for changed in (
            {"component_coordinate": "component://../bad"},
            {"state": "source-authoritative"},
            {"transition": "source-inventory"},
            {"source_snapshot_identity": self.source.uri},
            {"component_revision_identity": self.source.uri},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises((TypeError, ValueError)):
                    replace(self.inventory, **changed)

    def test_transitions_cannot_skip_states(self) -> None:
        with self.assertRaises(AuthorityTransitionError):
            ComponentAuthorityLifecycle.accept(
                self.inventory, evidence_identities=(identity("5"),)
            )
        with self.assertRaises(AuthorityTransitionError):
            ComponentAuthorityLifecycle.qualify(
                self.derived,
                target_lock_identity=identity("6"),
                generation_closure=self._closure(),
                verifier_identity=identity("b"),
                policy_identity=identity("c"),
                evidence_identities=(identity("d"),),
            )

    def test_transitions_reproduce_from_exact_evidence(self) -> None:
        repeated = ComponentAuthorityLifecycle.derive(
            self.inventory,
            component_revision_identity=self.revision,
            specification_set_identity=self.specification,
            evidence_identities=(self.specification,),
        )
        self.assertEqual(repeated, self.derived)
        self.assertEqual(repeated.identity, self.derived.identity)

    def test_qualification_drift_invalidates_effective_authority(self) -> None:
        qualified = ComponentAuthorityLifecycle.qualify(
            self.accepted,
            target_lock_identity=identity("6"),
            generation_closure=self._closure(),
            verifier_identity=identity("b"),
            policy_identity=identity("c"),
            evidence_identities=(identity("d"),),
        )
        current = ComponentAuthorityLifecycle.status(
            qualified,
            component_revision_identity=self.revision,
            target_lock_identity=identity("6"),
            generation_closure=self._closure(),
            verifier_identity=identity("b"),
            policy_identity=identity("c"),
        )
        self.assertEqual(current.blockers, ())
        stale = ComponentAuthorityLifecycle.status(
            qualified,
            component_revision_identity=self.revision,
            target_lock_identity=identity("6"),
            generation_closure=self._closure(),
            verifier_identity=identity("e"),
            policy_identity=identity("c"),
        )
        self.assertEqual(
            stale.effective_state, ComponentAuthorityState.DERIVED_SOURCE_RETAINED
        )
        self.assertIn("verifier-changed", stale.blockers)
        invalidated = ComponentAuthorityLifecycle.invalidate(
            qualified,
            evidence_identities=(identity("e"),),
        )
        self.assertEqual(
            invalidated.state, ComponentAuthorityState.DERIVED_SOURCE_RETAINED
        )
        self.assertIsNone(invalidated.verifier_identity)
        self.assertIsNone(invalidated.policy_identity)
        self.assertIsNone(invalidated.generation_closure)

    def test_store_is_append_only_and_retains_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline.c"
            baseline.write_text("int main(void) { return 0; }\n")
            store = FileAuthorityProjectionStore(root)
            store.append(self.inventory)
            store.append(self.derived)
            store.append(self.accepted)
            self.assertEqual(
                store.history("component://example/app"),
                (self.inventory, self.derived, self.accepted),
            )
            self.assertTrue(baseline.is_file())
            with self.assertRaises(AuthorityProjectionStoreError):
                store.append(self.derived)
            projection = (
                store._component_root("component://example/app", create=False)
                / "projections"
                / f"{self.derived.identity.digest}.json"
            )
            value = json.loads(projection.read_text())
            value["state"] = "source-authoritative"
            projection.write_text(json.dumps(value))
            with self.assertRaises(AuthorityProjectionStoreError):
                store.history("component://example/app")

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

    def test_changed_retained_revision_is_not_current_acceptance(self) -> None:
        stale = ComponentAuthorityLifecycle.status(
            self.accepted, component_revision_identity=identity("e")
        )
        self.assertEqual(
            stale.effective_state, ComponentAuthorityState.SOURCE_AUTHORITATIVE
        )
        self.assertIn("component-revision-changed", stale.blockers)

    def test_unmeasured_qualified_inputs_fail_closed_without_false_drift(
        self,
    ) -> None:
        qualified = ComponentAuthorityLifecycle.qualify(
            self.accepted,
            target_lock_identity=identity("6"),
            generation_closure=self._closure(),
            verifier_identity=identity("b"),
            policy_identity=identity("c"),
            evidence_identities=(identity("d"),),
        )
        status = ComponentAuthorityLifecycle.status(
            qualified, component_revision_identity=self.revision
        )
        self.assertEqual(
            status.effective_state, ComponentAuthorityState.DERIVED_SOURCE_RETAINED
        )
        self.assertEqual(
            status.blockers,
            (
                "target-lock-not-evaluated",
                "generation-closure-not-evaluated",
                "verifier-not-evaluated",
                "policy-not-evaluated",
            ),
        )

    def test_store_rejects_all_pre_v2_fungible_projections_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileAuthorityProjectionStore(Path(temporary))
            for projection in (self.inventory, self.derived, self.accepted):
                store.append(projection)
            qualified = ComponentAuthorityLifecycle.qualify(
                self.accepted,
                target_lock_identity=identity("6"),
                generation_closure=self._closure(),
                verifier_identity=identity("b"),
                policy_identity=identity("c"),
                evidence_identities=(identity("d"),),
            )
            with self.assertRaises(AuthorityProjectionStoreError) as raised:
                store.append(qualified)
            self.assertIn("qualification-v2-required", str(raised.exception))
            self.assertEqual(
                store.current(self.accepted.component_coordinate), self.accepted
            )

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

    @staticmethod
    def _closure() -> ComponentGenerationClosure:
        return ComponentGenerationClosure(
            identity("7"),
            identity("8"),
            identity("9"),
            identity("a"),
            identity("b"),
            identity("c"),
            identity("d"),
        )


if __name__ == "__main__":
    unittest.main()
