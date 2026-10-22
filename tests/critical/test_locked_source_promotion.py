"""Current Component-lock binding for regenerative source promotion."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

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
    ComponentAuthorityState,
    ComponentGenerationClosure,
    ContentIdentity,
    StandardProjectLifecycleDriver,
    canonical_identity,
)
from literate_ai.source_to_specification import (
    PromotionInputKind,
    QualificationStandardRebindMigration,
    SourcePromotionError,
    SourcePromotionInput,
    SourcePromotionMaterializer,
    VerifiedSourcePromotionEvidence,
)
from literate_ai.source_to_specification.promotion_materialization import (
    SOURCE_PROMOTION_PROVENANCE_SCHEMA,
    verify_source_promotion_evidence,
)
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleResult,
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

    def fresh_evidence(self, qualified):
        fresh_runs = tuple(
            replace(
                run,
                workspace_allocation_identity=_identity(
                    f"recertification-workspace-{index}"
                ),
            )
            for index, run in enumerate(self.qualification.runs)
        )
        fresh_qualification = QualificationLifecycleResult(
            fresh_runs,
            self.qualification.case_map,
            self.qualification.parity_evidence,
        )
        self.assertNotEqual(fresh_qualification.identity, self.qualification.identity)
        return replace(
            self.evidence,
            authority_projection=qualified,
            generation_closure=replace(
                self.closure,
                qualification_evidence_identity=fresh_qualification.identity,
            ),
            qualification_lifecycle_result=fresh_qualification,
        )

    def test_standard_rebind_migration_reopens_and_binds_old_and_new_runs(self):
        root = Path(self.temporary.name) / "rebound"
        root.mkdir()
        component_content = (self.component / "component.md").read_bytes()
        (root / "component.md").write_bytes(component_content)

        prior_driver = StandardProjectLifecycleDriver(
            _identity("prior-framework"), _identity("standard-policy")
        )
        current_driver = StandardProjectLifecycleDriver(
            _identity("current-framework"), prior_driver.policy_identity
        )

        def manifest(driver: StandardProjectLifecycleDriver) -> bytes:
            return json.dumps(
                {
                    "schema": "fixture-project",
                    "lifecycle_driver": driver.to_dict(),
                    "test_receipt_policy": {
                        "schema": "fixture-receipt",
                        "runner_identity": driver.identity.to_dict(),
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()

        prior_manifest = manifest(prior_driver)
        current_manifest = manifest(current_driver)
        configuration = root / "literate.project.json"
        configuration.write_bytes(prior_manifest)
        audit = SourcePromotionMaterializer().audit(
            (
                SourcePromotionInput(
                    PromotionInputKind.COMPONENT_INTENT,
                    root,
                    "promoted",
                    "component.md",
                    "component.md",
                    "sha256:" + hashlib.sha256(component_content).hexdigest(),
                ),
                SourcePromotionInput(
                    PromotionInputKind.PROJECT_CONFIGURATION,
                    root,
                    "promoted",
                    "literate.project.json",
                    "literate.project.json",
                    "sha256:" + hashlib.sha256(prior_manifest).hexdigest(),
                ),
            )
        )

        def qualification_for(driver: StandardProjectLifecycleDriver, label: str):
            runs = tuple(
                replace(
                    run,
                    framework_distribution_identity=(
                        driver.framework_distribution_identity
                    ),
                    lifecycle_policy_identity=driver.policy_identity,
                    driver_identity=driver.identity,
                    generation_input_audit_identity=ContentIdentity.parse_uri(
                        audit.identity
                    ),
                    promotion_tree_identity=ContentIdentity.parse_uri(
                        audit.materialized_tree_identity
                    ),
                    workspace_allocation_identity=_identity(label + str(index)),
                )
                for index, run in enumerate(self.qualification.runs)
            )
            return QualificationLifecycleResult(
                runs,
                self.qualification.case_map,
                self.qualification.parity_evidence,
            )

        prior_qualification = qualification_for(prior_driver, "prior-workspace")
        prior_closure = replace(
            self.closure,
            promotion_input_audit_identity=ContentIdentity.parse_uri(audit.identity),
            promotion_tree_identity=ContentIdentity.parse_uri(
                audit.materialized_tree_identity
            ),
            qualification_evidence_identity=prior_qualification.identity,
        )
        prior_evidence = replace(
            self.evidence,
            promotion_input_audits=(audit,),
            generation_closure=prior_closure,
            policy_identity=prior_driver.policy_identity,
            qualification_lifecycle_result=prior_qualification,
        )
        prior_qualified = qualify_locked_source_promotion(
            self.authority, prior_evidence
        )

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
                    "path": (
                        "generation-input-audits/"
                        + audit.identity.split(":", 1)[1]
                        + ".json"
                    ),
                }
            ],
        }
        record_identity = canonical_identity(record)
        provenance = root / "provenance" / "source-promotion" / record_identity.digest
        audit_path = provenance / record["generation_input_audit_references"][0]["path"]
        audit_path.parent.mkdir(parents=True)
        (provenance / "reference.json").write_text(json.dumps(record))
        audit_path.write_text(json.dumps(audit.to_dict()))
        prior_qualified = replace(
            prior_qualified, provenance_reference_identity=record_identity
        )
        qualification_directory = root / "provenance" / "qualification"
        qualification_directory.mkdir()
        (
            qualification_directory / f"{prior_qualification.identity.digest}.json"
        ).write_text(json.dumps(prior_qualification.to_dict()))

        configuration.write_bytes(current_manifest)
        migration = QualificationStandardRebindMigration(
            component_coordinate=prior_qualified.component_coordinate,
            prior_qualified_projection_identity=prior_qualified.identity.uri,
            generation_input_audit_identity=audit.identity,
            prior_project_configuration_base64=base64.b64encode(prior_manifest).decode(
                "ascii"
            ),
            current_project_configuration_identity=(
                "sha256:" + hashlib.sha256(current_manifest).hexdigest()
            ),
        )
        migration_directory = root / "provenance" / "qualification-rebind"
        migration_directory.mkdir()
        (migration_directory / f"{migration.identity.digest}.json").write_text(
            json.dumps(migration.to_dict())
        )

        reopened_prior = verify_source_promotion_evidence(
            root,
            prior_qualified,
            prior_project_configuration=prior_manifest,
        )
        self.assertEqual(reopened_prior.standard_rebind_migration, migration)

        current_qualification = qualification_for(current_driver, "current-workspace")
        current_closure = replace(
            prior_closure,
            qualification_evidence_identity=current_qualification.identity,
        )
        current_qualified = replace(
            prior_qualified,
            generation_closure=current_closure,
            evidence_identities=(
                *prior_qualified.evidence_identities,
                migration.identity,
            ),
        )
        (
            qualification_directory / f"{current_qualification.identity.digest}.json"
        ).write_text(json.dumps(current_qualification.to_dict()))
        reopened_current = verify_source_promotion_evidence(root, current_qualified)
        self.assertEqual(reopened_current.standard_rebind_migration, migration)

        stale_qualification = qualification_for(prior_driver, "stale-workspace")
        stale_projection = replace(
            current_qualified,
            generation_closure=replace(
                current_closure,
                qualification_evidence_identity=stale_qualification.identity,
            ),
        )
        (
            qualification_directory / f"{stale_qualification.identity.digest}.json"
        ).write_text(json.dumps(stale_qualification.to_dict()))
        with self.assertRaises(SourcePromotionError) as raised:
            verify_source_promotion_evidence(root, stale_projection)
        self.assertEqual(
            raised.exception.code,
            "promotion.rebind_migration_binding_mismatch",
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

    def test_store_atomically_recertifies_an_existing_qualified_head(self) -> None:
        store = FileAuthorityProjectionStore(Path(self.temporary.name))
        for projection in (self.inventory, self.assisted, self.retained):
            store.append(projection)
        store.append_qualified(self.authority, self.evidence)
        previous = store.current(self.retained.component_coordinate)

        recertified = store.recertify_qualified(
            self.authority,
            self.fresh_evidence(previous),
        )

        self.assertEqual(store.current(previous.component_coordinate), recertified)
        history = store.history(previous.component_coordinate)
        self.assertEqual(history[-3], previous)
        self.assertEqual(
            history[-2].state, ComponentAuthorityState.DERIVED_SOURCE_RETAINED
        )
        self.assertEqual(
            history[-1].state,
            ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE,
        )
        self.assertEqual(history[-2].prior_projection_identity, previous.identity)
        self.assertEqual(
            history[-1].prior_projection_identity,
            history[-2].identity,
        )

        with self.assertRaisesRegex(
            AuthorityProjectionStoreError, "current immutable history"
        ):
            store.recertify_qualified(
                self.authority,
                self.fresh_evidence(previous),
            )
        self.assertEqual(store.current(previous.component_coordinate), recertified)
        self.assertEqual(store.history(previous.component_coordinate), history)

    def test_recertification_write_failure_keeps_old_qualified_head(self) -> None:
        store = FileAuthorityProjectionStore(Path(self.temporary.name))
        for projection in (self.inventory, self.assisted, self.retained):
            store.append(projection)
        store.append_qualified(self.authority, self.evidence)
        previous = store.current(self.retained.component_coordinate)
        original_write = store._atomic_write
        projection_writes = 0

        def fail_second_projection(path, content, *, exclusive=False):
            nonlocal projection_writes
            if path.parent.name == "projections":
                projection_writes += 1
                if projection_writes == 2:
                    raise OSError("injected projection write failure")
            return original_write(path, content, exclusive=exclusive)

        with (
            mock.patch.object(
                store, "_atomic_write", side_effect=fail_second_projection
            ),
            self.assertRaisesRegex(AuthorityProjectionStoreError, "append lock failed"),
        ):
            store.recertify_qualified(
                self.authority,
                self.fresh_evidence(previous),
            )
        self.assertEqual(store.current(previous.component_coordinate), previous)


if __name__ == "__main__":
    unittest.main()
