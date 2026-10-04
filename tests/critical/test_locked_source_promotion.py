"""Current Component-lock binding for regenerative source promotion."""

from __future__ import annotations

import hashlib
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
)
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts import (
    ComponentGenerationClosure,
    ContentIdentity,
    canonical_identity,
)
from literate_ai.source_to_specification import (
    PromotionInputKind,
    SourcePromotionInput,
    SourcePromotionMaterializer,
    VerifiedSourcePromotionEvidence,
)

# Import the module, not the TestCase class, so unittest does not collect and
# rerun the fixture's own test methods as part of this module.
from tests.support import fixtures_test_qualification_lifecycle_runner as lifecycle
from tests.support.fixtures_test_component_lock_planning import _fixture
from tests.support.fixtures_test_locked_generation_authority import (
    _SELECTORS,
    _TARGET,
    _write_lock,
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
        lifecycle_fixture = lifecycle.QualificationLifecycleRunnerTests()
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


if __name__ == "__main__":
    unittest.main()
