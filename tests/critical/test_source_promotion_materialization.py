from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.contracts import ContentIdentity, StandardProjectLifecycleDriver
from literate_ai.source_to_specification.promotion_materialization import (
    GenerationInputAudit,
    PromotionInputKind,
    QualificationStandardRebindMigration,
    SourcePromotionError,
    SourcePromotionInput,
    SourcePromotionMaterializer,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def content_identity(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class SourcePromotionMaterializerTests(unittest.TestCase):
    def test_standard_rebind_migration_accepts_only_exact_binding_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()

            def identity(label: str) -> ContentIdentity:
                return ContentIdentity.parse_uri(content_identity(label.encode()))

            prior_driver = StandardProjectLifecycleDriver(
                identity("prior-distribution"), identity("policy")
            )
            current_driver = StandardProjectLifecycleDriver(
                identity("current-distribution"), identity("policy")
            )

            def manifest(driver: StandardProjectLifecycleDriver) -> dict[str, object]:
                return {
                    "schema": "fixture-project",
                    "project_id": "fixture",
                    "lifecycle_driver": driver.to_dict(),
                    "test_receipt_policy": {
                        "schema": "fixture-receipt",
                        "suite_id": "fixture",
                        "runner_identity": driver.identity.to_dict(),
                    },
                    "unchanged_authority": {"value": True},
                }

            prior_content = json.dumps(
                manifest(prior_driver), sort_keys=True, separators=(",", ":")
            ).encode()
            current_content = json.dumps(
                manifest(current_driver), sort_keys=True, separators=(",", ":")
            ).encode()
            configuration = project / "literate.project.json"
            configuration.write_bytes(prior_content)
            audit = SourcePromotionMaterializer().audit(
                (
                    SourcePromotionInput(
                        kind=PromotionInputKind.PROJECT_CONFIGURATION,
                        source_root=project,
                        source_root_label="project",
                        source_path="literate.project.json",
                        target_path="literate.project.json",
                        expected_content_identity=content_identity(prior_content),
                    ),
                )
            )

            def migration_for(
                prior: bytes, current: bytes
            ) -> QualificationStandardRebindMigration:
                return QualificationStandardRebindMigration(
                    component_coordinate="component://fixture/library",
                    prior_qualified_projection_identity=identity("qualified").uri,
                    generation_input_audit_identity=audit.identity,
                    prior_project_configuration_base64=base64.b64encode(prior).decode(
                        "ascii"
                    ),
                    current_project_configuration_identity=content_identity(current),
                )

            def require_error(
                code: str,
                *,
                prior: bytes = prior_content,
                current: bytes,
            ) -> None:
                configuration.write_bytes(current)
                with self.assertRaises(SourcePromotionError) as raised:
                    audit.verify(
                        project,
                        standard_rebind_migration=migration_for(prior, current),
                    )
                self.assertEqual(raised.exception.code, code)

            configuration.write_bytes(current_content)
            migration = migration_for(prior_content, current_content)

            audit.verify(project, standard_rebind_migration=migration)
            self.assertEqual(
                QualificationStandardRebindMigration.from_dict(migration.to_dict()),
                migration,
            )

            require_error(
                "promotion.rebind_migration_unnecessary",
                current=prior_content,
            )

            different_prior_content = json.dumps(
                manifest(
                    StandardProjectLifecycleDriver(
                        identity("different-prior-distribution"), identity("policy")
                    )
                ),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            require_error(
                "promotion.rebind_migration_mismatch",
                prior=different_prior_content,
                current=current_content,
            )

            changed_policy_content = json.dumps(
                manifest(
                    StandardProjectLifecycleDriver(
                        identity("current-distribution"), identity("changed-policy")
                    )
                ),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            require_error(
                "promotion.rebind_migration_policy_changed",
                current=changed_policy_content,
            )

            wrong_prior_runner = manifest(prior_driver)
            wrong_prior_runner["test_receipt_policy"]["runner_identity"] = (
                current_driver.identity.to_dict()
            )
            wrong_prior_runner_content = json.dumps(
                wrong_prior_runner, sort_keys=True, separators=(",", ":")
            ).encode()
            configuration.write_bytes(wrong_prior_runner_content)
            wrong_prior_runner_audit = SourcePromotionMaterializer().audit(
                (
                    SourcePromotionInput(
                        kind=PromotionInputKind.PROJECT_CONFIGURATION,
                        source_root=project,
                        source_root_label="project",
                        source_path="literate.project.json",
                        target_path="literate.project.json",
                        expected_content_identity=content_identity(
                            wrong_prior_runner_content
                        ),
                    ),
                )
            )
            configuration.write_bytes(current_content)
            wrong_prior_runner_migration = QualificationStandardRebindMigration(
                component_coordinate=migration.component_coordinate,
                prior_qualified_projection_identity=(
                    migration.prior_qualified_projection_identity
                ),
                generation_input_audit_identity=wrong_prior_runner_audit.identity,
                prior_project_configuration_base64=base64.b64encode(
                    wrong_prior_runner_content
                ).decode("ascii"),
                current_project_configuration_identity=content_identity(
                    current_content
                ),
            )
            with self.assertRaises(SourcePromotionError) as raised:
                wrong_prior_runner_audit.verify(
                    project,
                    standard_rebind_migration=wrong_prior_runner_migration,
                )
            self.assertEqual(
                raised.exception.code, "promotion.rebind_migration_invalid"
            )

            wrong_current_runner = manifest(current_driver)
            wrong_current_runner["test_receipt_policy"]["runner_identity"] = (
                prior_driver.identity.to_dict()
            )
            wrong_current_runner_content = json.dumps(
                wrong_current_runner, sort_keys=True, separators=(",", ":")
            ).encode()
            require_error(
                "promotion.rebind_migration_invalid",
                current=wrong_current_runner_content,
            )

            changed = manifest(current_driver)
            changed["unchanged_authority"] = {"value": False}
            changed_content = json.dumps(
                changed, sort_keys=True, separators=(",", ":")
            ).encode()
            require_error(
                "promotion.rebind_migration_scope_exceeded",
                current=changed_content,
            )

    def test_unlisted_source_prompt_and_journal_canaries_never_enter_closure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            specification = b"# Portable greeter\n\nPrint exactly hello.\n"
            component = b'{"schema":"component","name":"greeter"}\n'
            (accepted / "spec.md").write_bytes(specification)
            (accepted / "component.json").write_bytes(component)
            (accepted / "baseline").mkdir()
            (accepted / "baseline" / "source.txt").write_text("SOURCE-CANARY")
            (accepted / "provenance").mkdir()
            (accepted / "provenance" / "prompt.txt").write_text("PROMPT-CANARY")
            (accepted / ".literate").mkdir()
            journal = accepted / ".literate" / "source-translation.json"
            journal.write_text(
                '{"schema":"legacy","journal":"JOURNAL-CANARY"}',
                encoding="utf-8",
            )
            inputs = (
                SourcePromotionInput(
                    kind=PromotionInputKind.COMPONENT_INTENT,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="component.json",
                    target_path="component.json",
                    expected_content_identity=content_identity(component),
                ),
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path="specs/behavior.md",
                    expected_content_identity=content_identity(specification),
                ),
            )

            first = SourcePromotionMaterializer().materialize(inputs, root / "first")
            serialized = json.dumps(first.audit.to_dict(), sort_keys=True)

            self.assertEqual(
                sorted(
                    path.relative_to(first.output_root).as_posix()
                    for path in first.output_root.rglob("*")
                    if path.is_file()
                ),
                ["component.json", "specs/behavior.md"],
            )
            for canary in ("SOURCE-CANARY", "PROMPT-CANARY", "JOURNAL-CANARY"):
                self.assertNotIn(canary, serialized)
                self.assertFalse(
                    any(
                        canary.encode() in path.read_bytes()
                        for path in first.output_root.rglob("*")
                        if path.is_file()
                    )
                )
            self.assertEqual(
                GenerationInputAudit.from_dict(first.audit.to_dict()), first.audit
            )
            SchemaCatalog().validate(
                "urn:literate-ai:schema:v2:generation-input-audit",
                first.audit.to_dict(),
            )

            # Non-authority history is invisible to both closure identities.
            journal.write_text(
                '{"schema":"legacy","journal":"A-DIFFERENT-JOURNAL-CANARY"}',
                encoding="utf-8",
            )
            (accepted / "baseline" / "source.txt").write_text("OTHER-SOURCE-CANARY")
            second = SourcePromotionMaterializer().materialize(inputs, root / "second")
            self.assertEqual(first.audit.identity, second.audit.identity)
            self.assertEqual(
                first.audit.materialized_tree_identity,
                second.audit.materialized_tree_identity,
            )

            # An admitted specification edit changes both identities.
            revised = specification + b"Exit successfully.\n"
            (accepted / "spec.md").write_bytes(revised)
            revised_inputs = (
                inputs[0],
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path="specs/behavior.md",
                    expected_content_identity=content_identity(revised),
                ),
            )
            third = SourcePromotionMaterializer().materialize(
                revised_inputs, root / "third"
            )
            self.assertNotEqual(first.audit.identity, third.audit.identity)
            self.assertNotEqual(
                first.audit.materialized_tree_identity,
                third.audit.materialized_tree_identity,
            )

    def test_rejects_traversal_forbidden_targets_and_overlapping_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            content = b"spec"
            (accepted / "spec.md").write_bytes(content)

            with self.assertRaises(SourcePromotionError) as traversal:
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="../source.py",
                    target_path="spec.md",
                    expected_content_identity=content_identity(content),
                )
            self.assertEqual(traversal.exception.code, "promotion.invalid_path")

            with self.assertRaises(SourcePromotionError) as forbidden:
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=accepted,
                    source_root_label="accepted",
                    source_path="spec.md",
                    target_path=".literate/source-translation.json",
                    expected_content_identity=content_identity(content),
                )
            self.assertEqual(forbidden.exception.code, "promotion.forbidden_target")

            item = SourcePromotionInput(
                kind=PromotionInputKind.SPECIFICATION,
                source_root=accepted,
                source_root_label="accepted",
                source_path="spec.md",
                target_path="spec.md",
                expected_content_identity=content_identity(content),
            )
            with self.assertRaises(SourcePromotionError) as overlap:
                SourcePromotionMaterializer().materialize(
                    (item,), accepted / "component"
                )
            self.assertEqual(overlap.exception.code, "promotion.path_overlap")


if __name__ == "__main__":
    unittest.main()
