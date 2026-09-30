"""Current Component-lock binding for regenerative source promotion."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.authority import (
    AuthorityProjectionStoreError,
    FileAuthorityProjectionStore,
)
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)
from literate_ai.application.source_promotion import (
    LockedSourcePromotionError,
    qualify_locked_source_promotion,
    verify_qualified_locked_source_promotion,
)
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts import (
    ComponentAuthorityState,
    ComponentAuthorityTransition,
    ComponentGenerationClosure,
    ContentIdentity,
    canonical_identity,
)
from literate_ai.source_to_specification import (
    PromotionInputKind,
    SourcePromotionError,
    SourcePromotionInput,
    SourcePromotionMaterializer,
    VerifiedSourcePromotionEvidence,
)
from tests.unit.test_component_lock_planning import _fixture
from tests.unit.test_locked_generation_authority import (
    _SELECTORS,
    _TARGET,
    _write_lock,
)
from tests.unit.test_qualification_lifecycle_runner import (
    QualificationLifecycleRunnerTests,
)


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


class LockedSourcePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        component, flavors = _fixture(root)
        self.component = component
        _write_lock(component, flavors)
        self.authority = (
            FilesystemLockedGenerationAuthorityReader()
            .read(
                component,
                target_name=_TARGET,
                flavor_selectors=_SELECTORS,
                flavor_roots=(flavors,),
            )
            .authority
        )
        root_node = next(
            node
            for node in self.authority.lock.nodes
            if node.revision.identity == self.authority.lock.root_revision
        )
        self.inventory = ComponentAuthorityLifecycle.inventory(
            component_coordinate=self.authority.root_authoring.coordinate.uri,
            source_snapshot_identity=_identity("source-snapshot"),
            provenance_reference_identity=_identity("promotion-provenance"),
            evidence_identities=(_identity("source-inventory"),),
        )
        self.assisted = ComponentAuthorityLifecycle.derive(
            self.inventory,
            component_revision_identity=root_node.revision.identity,
            specification_set_identity=(root_node.revision.specification_set_identity),
            evidence_identities=(_identity("specification-derivation"),),
        )
        self.retained = ComponentAuthorityLifecycle.accept(
            self.assisted,
            evidence_identities=(_identity("human-acceptance"),),
        )

        component_bytes = (component / "component.md").read_bytes()
        audit = SourcePromotionMaterializer().audit(
            (
                SourcePromotionInput(
                    PromotionInputKind.COMPONENT_INTENT,
                    component,
                    "accepted-component",
                    "component.md",
                    "component.md",
                    "sha256:" + hashlib.sha256(component_bytes).hexdigest(),
                ),
            )
        )
        lifecycle_fixture = QualificationLifecycleRunnerTests()
        lifecycle_fixture.setUp()
        qualification_plan = replace(
            lifecycle_fixture.plan,
            target_profile_identity=self.authority.lock.target_profile_identity,
            component_lock_identity=self.authority.lock.identity,
            specification_set_identity=root_node.revision.specification_set_identity,
            source_snapshot_identity=self.retained.source_snapshot_identity,
            generation_input_audit_identity=ContentIdentity.parse_uri(audit.identity),
            promotion_tree_identity=ContentIdentity.parse_uri(
                audit.materialized_tree_identity
            ),
        )
        self.qualification = lifecycle_fixture.runner().run(qualification_plan)
        qualification_identity = self.qualification.identity
        self.closure = ComponentGenerationClosure(
            flavor_set_identity=_identity("selected-flavor-set"),
            skill_set_identity=_identity("selected-skill-set"),
            workflow_identity=_identity("workflow"),
            routing_policy_identity=_identity("routing-policy"),
            promotion_input_audit_identity=ContentIdentity.parse_uri(audit.identity),
            promotion_tree_identity=ContentIdentity.parse_uri(
                audit.materialized_tree_identity
            ),
            qualification_evidence_identity=qualification_identity,
        )
        self.evidence = VerifiedSourcePromotionEvidence(
            promotion_input_audits=(audit,),
            authority_projection=self.retained,
            target_lock_identity=self.authority.lock.identity,
            generation_closure=self.closure,
            verifier_identity=self.qualification.case_map.verifier_identity,
            policy_identity=self.qualification.runs[0].lifecycle_policy_identity,
            qualification_lifecycle_result=self.qualification,
        )

    def test_persisted_qualification_reopens_only_under_its_typed_identity(self):
        from literate_ai.source_to_specification.promotion_materialization import (
            SOURCE_PROMOTION_PROVENANCE_SCHEMA,
            verify_source_promotion_evidence,
        )

        root = Path(self.temporary.name).resolve() / "promoted"
        root.mkdir()
        (root / "component.md").write_bytes(
            (self.component / "component.md").read_bytes()
        )
        audit = self.evidence.promotion_input_audits[0]
        audit_name = f"generation-input-audits/{audit.identity.split(':')[1]}.json"
        record = {
            "schema": SOURCE_PROMOTION_PROVENANCE_SCHEMA,
            "source_snapshot_identity": self.retained.source_snapshot_identity.uri,
            "specification_set_identity": self.retained.specification_set_identity.uri,
            "review_identity": _identity("review").uri,
            "component_graph_identity": _identity("graph").uri,
            "translation_identity": None,
            "inverse_evidence_reference": None,
            "generation_input_audit_references": [
                {
                    "kind": "generation-input-audit",
                    "identity": audit.identity,
                    "path": audit_name,
                }
            ],
        }
        identity = canonical_identity(record)
        provenance = root / "provenance" / "source-promotion" / identity.digest
        (provenance / "generation-input-audits").mkdir(parents=True)
        (provenance / "reference.json").write_text(json.dumps(record))
        (provenance / audit_name).write_text(json.dumps(audit.to_dict()))
        qualified = replace(
            qualify_locked_source_promotion(self.authority, self.evidence),
            provenance_reference_identity=identity,
        )
        directory = root / "provenance" / "qualification"
        directory.mkdir()
        for expected in (self.qualification.identity, _identity("foreign-result")):
            (directory / f"{expected.digest}.json").write_text(
                json.dumps(self.qualification.to_dict())
            )
            projection = replace(
                qualified,
                generation_closure=replace(
                    self.closure, qualification_evidence_identity=expected
                ),
            )
            if expected == self.qualification.identity:
                reopened = verify_source_promotion_evidence(root, projection)
                self.assertEqual(
                    reopened.qualification_lifecycle_result, self.qualification
                )
                self.assertIs(
                    verify_qualified_locked_source_promotion(
                        self.authority,
                        reopened,
                        current_generation_closure=self.closure,
                        current_verifier_identity=self.qualification.case_map.verifier_identity,
                        current_policy_identity=self.qualification.runs[
                            0
                        ].lifecycle_policy_identity,
                    ),
                    projection,
                )
            else:
                with self.assertRaises(SourcePromotionError) as raised:
                    verify_source_promotion_evidence(root, projection)
                self.assertEqual(
                    raised.exception.code, "promotion.qualification_evidence_mismatch"
                )

    def test_existing_qualified_projection_rechecks_current_inputs_without_transition(
        self,
    ) -> None:
        qualified = qualify_locked_source_promotion(self.authority, self.evidence)
        evidence = replace(self.evidence, authority_projection=qualified)
        current = dict(
            current_generation_closure=self.closure,
            current_verifier_identity=self.qualification.case_map.verifier_identity,
            current_policy_identity=self.qualification.runs[
                0
            ].lifecycle_policy_identity,
        )
        self.assertIs(
            verify_qualified_locked_source_promotion(
                self.authority, evidence, **current
            ),
            qualified,
        )
        for changes in (
            {
                "current_generation_closure": replace(
                    self.closure, workflow_identity=_identity("new-workflow")
                )
            },
            {"current_verifier_identity": _identity("new-verifier")},
            {"current_policy_identity": _identity("new-policy")},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(LockedSourcePromotionError) as raised,
            ):
                verify_qualified_locked_source_promotion(
                    self.authority, evidence, **{**current, **changes}
                )
            self.assertEqual(
                raised.exception.code, "promotion.qualified_authority_stale"
            )
        with self.assertRaises(TypeError):
            verify_qualified_locked_source_promotion(
                self.authority,
                evidence,
                **{**current, "current_generation_closure": None},
            )
        with self.assertRaises(LockedSourcePromotionError) as raised:
            verify_qualified_locked_source_promotion(
                self.authority, self.evidence, **current
            )
        self.assertEqual(raised.exception.code, "promotion.lifecycle_state_invalid")
        with self.assertRaises(LockedSourcePromotionError):
            qualify_locked_source_promotion(self.authority, evidence)

    def test_qualified_projection_cannot_borrow_another_reopened_closure(self) -> None:
        qualified = qualify_locked_source_promotion(self.authority, self.evidence)
        changed_closure = replace(
            self.closure, workflow_identity=_identity("different-workflow")
        )
        changed = replace(qualified, generation_closure=changed_closure)
        with self.assertRaises(LockedSourcePromotionError) as raised:
            verify_qualified_locked_source_promotion(
                self.authority,
                replace(self.evidence, authority_projection=changed),
                current_generation_closure=changed_closure,
                current_verifier_identity=self.evidence.verifier_identity,
                current_policy_identity=self.evidence.policy_identity,
            )
        self.assertEqual(
            raised.exception.code, "promotion.qualification_evidence_drift"
        )

    def test_rehashed_qualification_runs_must_keep_the_current_component_lock(
        self,
    ) -> None:
        qualification = replace(
            self.qualification,
            runs=tuple(
                replace(run, component_lock_identity=_identity("foreign-lock"))
                for run in self.qualification.runs
            ),
        )
        closure = replace(
            self.closure, qualification_evidence_identity=qualification.identity
        )
        evidence = replace(
            self.evidence,
            generation_closure=closure,
            qualification_lifecycle_result=qualification,
        )
        with self.assertRaises(LockedSourcePromotionError) as raised:
            qualify_locked_source_promotion(self.authority, evidence)
        self.assertEqual(
            raised.exception.code, "promotion.qualification_evidence_drift"
        )

    def test_legacy_current_qualification_cannot_authorize_fungibility(self) -> None:
        legacy = replace(
            self.evidence,
            qualification_lifecycle_result=None,
        )
        with self.assertRaises(LockedSourcePromotionError) as raised:
            qualify_locked_source_promotion(self.authority, legacy)
        self.assertEqual(
            raised.exception.code,
            "promotion.qualification_contract_legacy",
        )

    def test_qualifies_only_under_the_exact_current_component_lock(self) -> None:
        qualified = qualify_locked_source_promotion(self.authority, self.evidence)

        self.assertEqual(
            qualified.state,
            ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE,
        )
        self.assertEqual(
            qualified.transition,
            ComponentAuthorityTransition.REGENERATIVE_QUALIFICATION,
        )
        self.assertEqual(
            qualified.target_lock_identity,
            self.authority.lock.identity,
        )
        self.assertEqual(qualified.generation_closure, self.closure)
        self.assertEqual(
            qualified.evidence_identities,
            self.evidence.qualification_evidence_identities,
        )
        qualified.require_successor_of(self.retained)

    def test_missing_and_stale_lock_evidence_fail_closed(self) -> None:
        with self.assertRaises(LockedSourcePromotionError) as missing:
            qualify_locked_source_promotion(
                self.authority,
                replace(self.evidence, target_lock_identity=None),
            )
        self.assertEqual(missing.exception.code, "promotion.lock_evidence_missing")

        with self.assertRaises(LockedSourcePromotionError) as stale:
            qualify_locked_source_promotion(
                self.authority,
                replace(
                    self.evidence,
                    target_lock_identity=_identity("superseded-component-lock"),
                ),
            )
        self.assertEqual(stale.exception.code, "promotion.lock_evidence_stale")

    def test_drifted_component_and_specification_facts_fail_closed(self) -> None:
        for field in (
            "component_revision_identity",
            "specification_set_identity",
        ):
            with self.subTest(field=field):
                drifted = replace(self.retained, **{field: _identity(field)})
                with self.assertRaises(LockedSourcePromotionError) as raised:
                    qualify_locked_source_promotion(
                        self.authority,
                        replace(self.evidence, authority_projection=drifted),
                    )
                self.assertEqual(raised.exception.code, "promotion.lock_evidence_drift")

    def test_lifecycle_facts_are_required_and_earlier_states_remain_unbound(
        self,
    ) -> None:
        for projection in (self.inventory, self.assisted):
            with self.subTest(state=projection.state):
                self.assertIsNone(projection.target_lock_identity)
                with self.assertRaises(LockedSourcePromotionError) as raised:
                    qualify_locked_source_promotion(
                        self.authority,
                        replace(
                            self.evidence,
                            authority_projection=projection,
                            target_lock_identity=None,
                        ),
                    )
                self.assertEqual(
                    raised.exception.code, "promotion.lifecycle_state_invalid"
                )

        self.assertIsNone(self.retained.target_lock_identity)
        with self.assertRaises(LockedSourcePromotionError) as no_projection:
            qualify_locked_source_promotion(
                self.authority,
                replace(self.evidence, authority_projection=None),
            )
        self.assertEqual(
            no_projection.exception.code, "promotion.lifecycle_evidence_missing"
        )
        with self.assertRaises(LockedSourcePromotionError) as no_closure:
            qualify_locked_source_promotion(
                self.authority,
                replace(self.evidence, generation_closure=None),
            )
        self.assertEqual(
            no_closure.exception.code,
            "promotion.qualification_evidence_missing",
        )

    def test_generation_closure_must_bind_the_verified_promotion_audit(self) -> None:
        with self.assertRaises(SourcePromotionError) as drifted:
            replace(
                self.evidence,
                generation_closure=replace(
                    self.closure,
                    promotion_tree_identity=_identity("another-promotion-tree"),
                ),
            )
        self.assertEqual(
            drifted.exception.code,
            "promotion.lifecycle_evidence_drift",
        )

    def test_store_admits_qualification_only_through_locked_service(self) -> None:
        store = FileAuthorityProjectionStore(Path(self.temporary.name))
        for projection in (self.inventory, self.assisted, self.retained):
            store.append(projection)
        qualified = qualify_locked_source_promotion(self.authority, self.evidence)

        with self.assertRaisesRegex(
            AuthorityProjectionStoreError,
            "locked lifecycle admission",
        ):
            store.append(qualified)

        path = store.append_qualified(self.authority, self.evidence)
        self.assertTrue(path.is_file())
        self.assertEqual(
            store.current(self.retained.component_coordinate),
            qualified,
        )

    def test_store_rejects_stale_lifecycle_head_after_lock_qualification(self) -> None:
        store = FileAuthorityProjectionStore(Path(self.temporary.name))
        for projection in (self.inventory, self.assisted, self.retained):
            store.append(projection)
        alternate_retained = ComponentAuthorityLifecycle.accept(
            self.assisted,
            evidence_identities=(_identity("different-human-acceptance"),),
        )

        with self.assertRaisesRegex(
            AuthorityProjectionStoreError,
            "current immutable history",
        ):
            store.append_qualified(
                self.authority,
                replace(
                    self.evidence,
                    authority_projection=alternate_retained,
                ),
            )


if __name__ == "__main__":
    unittest.main()
