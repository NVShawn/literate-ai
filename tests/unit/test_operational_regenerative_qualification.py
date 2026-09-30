from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.application import SourcePromotionService
from literate_ai.source_to_specification import (
    LEGACY_QUALIFICATION_BLOCKER,
    CurrentRegenerativeQualificationEvidence,
    LocalFilesystemQualificationCheckpointStore,
    ParityOutcome,
    PromotionInputKind,
    QualificationRunCheckpoint,
    RegenerationOutcome,
    RegenerationRunPlan,
    RegenerativeQualificationPolicy,
    SourcePromotionInput,
    SourceToSpecificationError,
    canonical_digest,
    inventory_source,
    run_regenerative_qualification,
)
from tests.unit.test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]


def identity(label: str) -> str:
    return canonical_digest({"fixture": label})


class FixtureRegenerator:
    provider_identity = identity("provider:regenerator")

    def __init__(self, *, extra_input: bool = False) -> None:
        self.extra_input = extra_input
        self.received_empty_workspaces: list[bool] = []

    def regenerate(self, request, workspace):
        self.received_empty_workspaces.append(not any(workspace.iterdir()))
        generated = workspace / "application.txt"
        generated.write_text("spec-only output\n", encoding="utf-8")
        inputs = request.declared_generation_inputs
        if self.extra_input:
            inputs = (*inputs, identity("undeclared-input"))
        return RegenerationOutcome(
            generation_input_ids=inputs,
            generated_tree_id=canonical_digest(generated.read_text()),
            build_result_id=identity(f"build:{request.run_id}"),
            generated_test_result_id=identity(f"tests:{request.run_id}"),
            generated_source_cache_hit=False,
            build_passed=True,
            generated_tests_total=2,
            generated_tests_succeeded=2,
            generated_tests_failed=0,
            generated_tests_skipped=0,
        )


class FixtureParityVerifier:
    provider_identity = identity("provider:parity")

    def verify(self, request, generated_workspace):
        passed = (generated_workspace / "application.txt").read_text() == (
            "spec-only output\n"
        )
        return ParityOutcome(
            identity(f"parity:{request.run_id}"),
            passed,
            request.covered_surface_ids,
        )


class FixtureAttestor:
    def __init__(self, provider_identity):
        self.provider_identity = provider_identity

    def attest(self, payload):
        return canonical_digest(payload)

    def verify(self, payload, attestation_id):
        return canonical_digest(payload) == attestation_id


class InterruptingRegenerator(FixtureRegenerator):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def regenerate(self, request, workspace):
        self.calls += 1
        if self.calls == 2:
            raise SourceToSpecificationError(
                "fixture.interrupted", "second generation was interrupted"
            )
        return super().regenerate(request, workspace)


class OperationalRegenerativeQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source_root = root / "source"
        self.accepted_root = root / "accepted"
        self.source_root.mkdir()
        self.accepted_root.mkdir()
        (self.source_root / "main.py").write_text(
            "ORIGINAL_SOURCE_CANARY = True\n", encoding="utf-8"
        )
        (self.accepted_root / "spec.md").write_text(
            "# Observable behavior\nReturn a greeting.\n", encoding="utf-8"
        )
        self.source_inventory = inventory_source(self.source_root)
        self.source = self.source_inventory.identity
        self.source_exclusion = self._assessment("spec.md", "spec.md")
        self.specification = identity("specification")
        self.target = identity("target")
        self.policy = RegenerativeQualificationPolicy(
            "operational-parity@1",
            2,
            (self.target,),
            ("cli", "errors"),
        )
        self.plans = tuple(
            RegenerationRunPlan(
                run_id=identity(f"run:{number}"),
                specification_set_id=self.specification,
                target_profile_id=self.target,
                flavor_lock_id=identity("flavors"),
                generation_recipe_id=identity("recipe"),
                covered_surface_ids=("cli", "errors"),
            )
            for number in range(2)
        )

    def _assessment(self, source_path: str, target_path: str):
        content = (self.accepted_root / source_path).read_bytes()
        audit = SourcePromotionService().audit(
            (
                SourcePromotionInput(
                    kind=PromotionInputKind.SPECIFICATION,
                    source_root=self.accepted_root,
                    source_root_label="accepted-specifications",
                    source_path=source_path,
                    target_path=target_path,
                    expected_content_identity=(
                        "sha256:" + hashlib.sha256(content).hexdigest()
                    ),
                ),
            )
        )
        return SourcePromotionService().assess_source_exclusion(
            audit, self.source_inventory
        )

    def qualify(
        self, regenerator=None, attestor_identity=None, *, source_exclusion="default"
    ):
        selected = regenerator or FixtureRegenerator()
        assessment = (
            self.source_exclusion if source_exclusion == "default" else source_exclusion
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = run_regenerative_qualification(
                source_snapshot_id=self.source,
                specification_set_id=self.specification,
                policy=self.policy,
                plans=self.plans,
                regenerator=selected,
                parity_verifier=FixtureParityVerifier(),
                attestor=FixtureAttestor(
                    attestor_identity or identity("provider:attestor")
                ),
                source_exclusion=assessment,
                scratch_root=Path(temporary),
            )
        return result, selected

    def test_runner_measures_empty_spec_only_runs_as_historical_evidence(self):
        result, regenerator = self.qualify()
        self.assertFalse(result.decision.qualified)
        self.assertIn(LEGACY_QUALIFICATION_BLOCKER, result.decision.blockers)
        self.assertEqual(regenerator.received_empty_workspaces, [True, True])
        self.assertTrue(
            all(item.source_excluded_from_generation for item in result.evidence)
        )
        self.assertEqual(len({item.run_attestation_id for item in result.evidence}), 2)

    def current_evidence(self, runs):
        return CurrentRegenerativeQualificationEvidence(
            source_snapshot_identity=self.source,
            specification_set_identity=self.specification,
            component_revision_identity=identity("component-revision"),
            target_lock_identity=identity("flavors"),
            flavor_set_identity=identity("flavor-set"),
            skill_set_identity=identity("skill-set"),
            workflow_identity=identity("workflow"),
            routing_policy_identity=identity("routing"),
            promotion_input_audit_identity=self.source_exclusion.generation_input_audit_identity,
            promotion_tree_identity=self.source_exclusion.materialized_tree_identity,
            inverse_evidence_custody_identity=identity("inverse-custody"),
            generation_recipe_identity=identity("recipe"),
            regenerator_identity=FixtureRegenerator.provider_identity,
            verifier_identity=FixtureParityVerifier.provider_identity,
            attestor_identity=identity("provider:attestor"),
            policy=self.policy,
            runs=runs,
        )

    def test_current_evidence_admits_complete_clean_runs(self):
        result, _ = self.qualify()
        evidence = self.current_evidence(result.evidence)
        self.assertEqual(
            evidence.to_dict()["runs"], [item.to_dict() for item in result.evidence]
        )
        self.assertTrue(evidence.identity.startswith("sha256:"))
        SchemaCatalog(ROOT / "schemas" / "v2").validate(
            evidence.SCHEMA, evidence.to_dict()
        )
        self.assertEqual(
            CurrentRegenerativeQualificationEvidence.from_dict(evidence.to_dict()),
            evidence,
        )

    def test_current_evidence_rejects_failed_or_cached_run(self):
        result, _ = self.qualify()
        for mutation in (
            {"build_passed": False},
            {"generated_source_cache_hit": True},
            {"independent_parity_passed": False},
        ):
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(
                    SourceToSpecificationError, "every admitted run"
                ),
            ):
                self.current_evidence(
                    (
                        replace(result.evidence[0], **mutation),
                        *result.evidence[1:],
                    )
                )

    def test_undeclared_generation_input_fails_before_attestation(self):
        with self.assertRaisesRegex(SourceToSpecificationError, "outside the exact"):
            self.qualify(FixtureRegenerator(extra_input=True))

    def test_missing_source_closure_evidence_fails_closed_instead_of_claiming_true(
        self,
    ):
        result, _ = self.qualify(source_exclusion=None)

        self.assertTrue(
            all(not item.source_excluded_from_generation for item in result.evidence)
        )
        self.assertIn("original-source-entered-generation", result.decision.blockers)

    def test_source_byte_identity_in_audited_closure_fails_closed(self):
        (self.accepted_root / "copied-spec.md").write_bytes(
            (self.source_root / "main.py").read_bytes()
        )
        assessment = self._assessment("copied-spec.md", "spec.md")

        self.assertFalse(assessment.source_excluded)
        self.assertEqual(
            assessment.overlapping_content_identities,
            (self.source_inventory.entries[0].content_digest,),
        )
        result, _ = self.qualify(source_exclusion=assessment)
        self.assertTrue(
            all(not item.source_excluded_from_generation for item in result.evidence)
        )

    def test_source_path_in_audited_closure_fails_even_with_different_bytes(self):
        (self.accepted_root / "main.py").write_text(
            "# specification-shaped but source-path aliased\n", encoding="utf-8"
        )
        assessment = self._assessment("main.py", "spec.md")

        self.assertFalse(assessment.source_excluded)
        self.assertEqual(assessment.overlapping_paths, ("main.py",))
        self.assertEqual(assessment.overlapping_content_identities, ())

    def test_assessment_for_another_source_inventory_is_rejected(self):
        with self.assertRaisesRegex(SourceToSpecificationError, "another source"):
            run_regenerative_qualification(
                source_snapshot_id=identity("other-source"),
                specification_set_id=self.specification,
                policy=self.policy,
                plans=self.plans,
                regenerator=FixtureRegenerator(),
                parity_verifier=FixtureParityVerifier(),
                attestor=FixtureAttestor(identity("provider:attestor")),
                source_exclusion=self.source_exclusion,
            )

    def test_interrupted_qualification_resumes_from_signed_run_evidence(self):
        checkpoint_root = Path(self.temporary.name) / "checkpoints"
        store = LocalFilesystemQualificationCheckpointStore(checkpoint_root)
        interrupted = InterruptingRegenerator()
        attestor = FixtureAttestor(identity("provider:attestor"))
        with self.assertRaisesRegex(SourceToSpecificationError, "interrupted"):
            run_regenerative_qualification(
                source_snapshot_id=self.source,
                specification_set_id=self.specification,
                policy=self.policy,
                plans=self.plans,
                regenerator=interrupted,
                parity_verifier=FixtureParityVerifier(),
                attestor=attestor,
                source_exclusion=self.source_exclusion,
                checkpoint_store=store,
            )
        self.assertEqual(interrupted.calls, 2)
        checkpoint_files = tuple(checkpoint_root.glob("*.json"))
        self.assertEqual(len(checkpoint_files), 1)
        checkpoint = QualificationRunCheckpoint.from_dict(
            json.loads(checkpoint_files[0].read_bytes())
        )
        SchemaCatalog(ROOT / "schemas" / "v2").validate(
            checkpoint.to_dict()["schema"], checkpoint.to_dict()
        )

        resumed = FixtureRegenerator()
        result = run_regenerative_qualification(
            source_snapshot_id=self.source,
            specification_set_id=self.specification,
            policy=self.policy,
            plans=self.plans,
            regenerator=resumed,
            parity_verifier=FixtureParityVerifier(),
            attestor=attestor,
            source_exclusion=self.source_exclusion,
            checkpoint_store=store,
        )

        self.assertEqual(len(resumed.received_empty_workspaces), 1)
        self.assertEqual(result.resumed_run_ids, (self.plans[0].run_id,))
        self.assertEqual(len(result.evidence), 2)

    def test_runner_rejects_nonindependent_providers(self):
        with self.assertRaisesRegex(SourceToSpecificationError, "distinct identities"):
            self.qualify(attestor_identity=FixtureParityVerifier.provider_identity)

    def test_plan_for_another_specification_is_rejected_before_execution(self):
        bad = replace(self.plans[0], specification_set_id=identity("other"))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                SourceToSpecificationError, "exact specification"
            ):
                run_regenerative_qualification(
                    source_snapshot_id=self.source,
                    specification_set_id=self.specification,
                    policy=self.policy,
                    plans=(bad, self.plans[1]),
                    regenerator=FixtureRegenerator(),
                    parity_verifier=FixtureParityVerifier(),
                    attestor=FixtureAttestor(identity("provider:attestor")),
                    scratch_root=Path(temporary),
                )


if __name__ == "__main__":
    unittest.main()
