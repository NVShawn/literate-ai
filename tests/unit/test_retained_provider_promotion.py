"""Persisted provider heads and evidence remain current through read-only admission."""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.authority import FileAuthorityProjectionStore
from literate_ai.adapters.retained_provider_promotion import (
    read_current_qualified_promotion,
)
from literate_ai.application.source_promotion import (
    LockedSourcePromotionError,
    SourcePromotionService,
)
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts import canonical_identity
from literate_ai.source_to_specification.promotion_materialization import (
    SOURCE_PROMOTION_PROVENANCE_SCHEMA,
    SourcePromotionError,
)
from tests.support import fixtures_test_locked_source_promotion as fixtures


class RetainedProviderPromotionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LockedSourcePromotionTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.root = Path(fixture.temporary.name).resolve() / "promoted"
        self.root.mkdir()
        (self.root / "component.md").write_bytes(
            (fixture.component / "component.md").read_bytes()
        )
        audit = fixture.evidence.promotion_input_audits[0]
        audit_name = f"generation-input-audits/{audit.identity.split(':')[1]}.json"
        record = {
            "schema": SOURCE_PROMOTION_PROVENANCE_SCHEMA,
            "source_snapshot_identity": fixture.retained.source_snapshot_identity.uri,
            "specification_set_identity": (
                fixture.retained.specification_set_identity.uri
            ),
            "review_identity": fixtures._identity("review").uri,
            "component_graph_identity": fixtures._identity("graph").uri,
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
        provenance_identity = canonical_identity(record)
        provenance = (
            self.root / "provenance/source-promotion" / provenance_identity.digest
        )
        (provenance / "generation-input-audits").mkdir(parents=True)
        (provenance / "reference.json").write_text(json.dumps(record))
        (provenance / audit_name).write_text(json.dumps(audit.to_dict()))
        directory = self.root / "provenance/qualification"
        directory.mkdir()
        self.qualification_path = (
            directory / f"{fixture.qualification.identity.digest}.json"
        )
        self.qualification_path.write_text(json.dumps(fixture.qualification.to_dict()))
        inventory = replace(
            fixture.inventory, provenance_reference_identity=provenance_identity
        )
        assisted = ComponentAuthorityLifecycle.derive(
            inventory,
            component_revision_identity=fixture.assisted.component_revision_identity,
            specification_set_identity=fixture.assisted.specification_set_identity,
            evidence_identities=fixture.assisted.evidence_identities,
        )
        retained = ComponentAuthorityLifecycle.accept(
            assisted, evidence_identities=fixture.retained.evidence_identities
        )
        self.store = FileAuthorityProjectionStore(self.root)
        for projection in (inventory, assisted, retained):
            self.store.append(projection)
        self.store.append_qualified(
            fixture.authority, replace(fixture.evidence, authority_projection=retained)
        )
        self.qualified = self.store.current(
            fixture.authority.root_authoring.coordinate.uri
        )
        self.current = {
            "current_generation_closure": fixture.closure,
            "current_verifier_identity": fixture.evidence.verifier_identity,
            "current_policy_identity": fixture.evidence.policy_identity,
        }

    def read(self, **changes):
        return read_current_qualified_promotion(
            self.root, self.fixture.authority, **{**self.current, **changes}
        )

    def inventory(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def invalidate(self):
        self.store.append(
            ComponentAuthorityLifecycle.invalidate(
                self.qualified,
                evidence_identities=(fixtures._identity("invalidation"),),
            )
        )

    def test_reopens_persisted_qualification_without_writes_or_new_transition(self):
        before = self.inventory()
        current = self.read()
        current.require_unchanged()
        self.assertEqual(current.evidence.authority_projection, self.qualified)
        self.assertEqual(
            current.evidence.qualification_lifecycle_result, self.fixture.qualification
        )
        self.assertEqual(self.inventory(), before)
        self.assertEqual(
            len(self.store.history(self.qualified.component_coordinate)), 4
        )

    def test_independent_current_policy_and_closure_refuse_historical_evidence(self):
        for changed in (
            {"current_policy_identity": fixtures._identity("new-policy")},
            {"current_verifier_identity": fixtures._identity("new-verifier")},
            {
                "current_generation_closure": replace(
                    self.fixture.closure,
                    workflow_identity=fixtures._identity("new-workflow"),
                )
            },
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaises(LockedSourcePromotionError),
            ):
                self.read(**changed)

    def test_audited_input_and_qualification_drift_refuse_without_cleanup(self):
        for path in (self.root / "component.md", self.qualification_path):
            with self.subTest(path=path.name):
                current = self.read()
                original = path.read_bytes()
                path.write_bytes(b"{}")
                before = self.inventory()
                try:
                    with self.assertRaises(SourcePromotionError):
                        current.require_unchanged()
                    self.assertEqual(self.inventory(), before)
                finally:
                    path.write_bytes(original)

    def test_later_invalidation_refuses_revalidation_and_preserves_new_head(self):
        current = self.read()
        self.invalidate()
        before = self.inventory()
        with self.assertRaises(LockedSourcePromotionError):
            current.require_unchanged()
        self.assertEqual(self.inventory(), before)

    def test_head_change_during_evidence_read_prevents_return(self):
        verify = SourcePromotionService.verify_evidence

        def invalidate_after_read(service, root, projection):
            evidence = verify(service, root, projection)
            self.invalidate()
            return evidence

        with (
            mock.patch.object(
                SourcePromotionService, "verify_evidence", invalidate_after_read
            ),
            self.assertRaisesRegex(ValueError, "promotion-changed"),
        ):
            self.read()
        self.assertEqual(
            len(self.store.history(self.qualified.component_coordinate)), 5
        )
