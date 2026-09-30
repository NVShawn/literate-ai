from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.application import SourcePromotionService
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts import canonical_identity
from literate_ai.source_to_specification import (
    SOURCE_PROMOTION_PROVENANCE_SCHEMA,
    PromotionInputKind,
    SourcePromotionInput,
    inventory_source,
)
from tests.unit.test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]


class SourcePromotionServiceTests(unittest.TestCase):
    def test_v3_evidence_reopens_content_addressed_inverse_custody(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            project = root / "project"
            accepted.mkdir()
            specification = b"# Contract\nEmit the documented result.\n"
            (accepted / "spec.md").write_bytes(specification)
            materialized = SourcePromotionService().materialize(
                (
                    SourcePromotionInput(
                        kind=PromotionInputKind.SPECIFICATION,
                        source_root=accepted,
                        source_root_label="accepted-specifications",
                        source_path="spec.md",
                        target_path="spec.md",
                        expected_content_identity=(
                            "sha256:" + hashlib.sha256(specification).hexdigest()
                        ),
                    ),
                ),
                project,
            )
            translation = {
                "schema": (
                    "urn:literate-ai:schema:v4:source-to-specification-translation-run"
                ),
                "mode": "source-intelligence-coding-cli",
                "intelligence": {"fixture": "retained"},
                "journals": [],
            }
            custody = {
                "schema": "urn:literate-ai:schema:v1:inverse-evidence-custody",
                "translation_identity": canonical_identity(translation).uri,
                "source_inventory": {"fixture": "retained"},
                "behavioral_surface_inventory": {"fixture": "retained"},
                "evidence_partition_manifest": {"fixture": "retained"},
                "evidence_batch_plan": {"fixture": "retained"},
            }
            source_snapshot = canonical_identity({"fixture": "source"})
            audit = materialized.audit
            record = {
                "schema": SOURCE_PROMOTION_PROVENANCE_SCHEMA,
                "source_snapshot_identity": source_snapshot.uri,
                "specification_set_identity": canonical_identity(
                    {"fixture": "specification"}
                ).uri,
                "review_identity": canonical_identity({"fixture": "review"}).uri,
                "component_graph_identity": canonical_identity(
                    {"fixture": "graph"}
                ).uri,
                "translation_identity": canonical_identity(translation).uri,
                "inverse_evidence_reference": {
                    "kind": "inverse-evidence-custody",
                    "identity": canonical_identity(custody).uri,
                    "path": "inverse-evidence.json",
                },
                "generation_input_audit_references": [
                    {
                        "kind": "generation-input-audit",
                        "identity": audit.identity,
                        "path": (
                            "generation-input-audits/"
                            f"{audit.identity.removeprefix('sha256:')}.json"
                        ),
                    }
                ],
            }
            promotion_identity = canonical_identity(record)
            schemas = SchemaCatalog(ROOT / "schemas" / "v2")
            schemas.validate(SOURCE_PROMOTION_PROVENANCE_SCHEMA, record)
            promotion_root = (
                project / "provenance" / "source-promotion" / promotion_identity.digest
            )
            audit_root = promotion_root / "generation-input-audits"
            audit_root.mkdir(parents=True)
            for name, value in (
                ("reference.json", record),
                ("source-translation.json", translation),
                ("inverse-evidence.json", custody),
            ):
                (promotion_root / name).write_text(json.dumps(value), encoding="utf-8")
            (audit_root / f"{audit.identity.removeprefix('sha256:')}.json").write_text(
                json.dumps(audit.to_dict()), encoding="utf-8"
            )
            projection = ComponentAuthorityLifecycle.inventory(
                component_coordinate="component://example/app",
                source_snapshot_identity=source_snapshot,
                provenance_reference_identity=promotion_identity,
                evidence_identities=(canonical_identity({"fixture": "inventory"}),),
            )

            verified = SourcePromotionService().verify_evidence(project, projection)

            self.assertEqual(verified.translation, translation)
            self.assertEqual(verified.inverse_evidence_custody, custody)
            self.assertIn(
                canonical_identity(custody),
                verified.qualification_evidence_identities,
            )
            (promotion_root / "inverse-evidence.json").write_text(
                json.dumps({**custody, "source_inventory": {"changed": True}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "content identity"):
                SourcePromotionService().verify_evidence(project, projection)

    def test_public_service_materializes_and_assesses_the_exact_audited_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            accepted = root / "accepted"
            source.mkdir()
            accepted.mkdir()
            (source / "application.py").write_text(
                "SOURCE_CANARY = 'must-not-cross'\n", encoding="utf-8"
            )
            specification = b"# Contract\nEmit the documented result.\n"
            (accepted / "spec.md").write_bytes(specification)
            item = SourcePromotionInput(
                kind=PromotionInputKind.SPECIFICATION,
                source_root=accepted,
                source_root_label="accepted-specifications",
                source_path="spec.md",
                target_path="spec.md",
                expected_content_identity=(
                    "sha256:" + hashlib.sha256(specification).hexdigest()
                ),
            )

            service = SourcePromotionService()
            result = service.materialize((item,), root / "component")
            assessment = service.assess_source_exclusion(
                result.audit, inventory_source(source)
            )

            self.assertTrue(assessment.source_excluded)
            self.assertEqual(
                tuple(
                    path.name
                    for path in result.output_root.rglob("*")
                    if path.is_file()
                ),
                ("spec.md",),
            )
            self.assertNotIn(
                b"must-not-cross", (result.output_root / "spec.md").read_bytes()
            )


if __name__ == "__main__":
    unittest.main()
