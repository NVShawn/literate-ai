from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from literate_ai.application import SourcePromotionService
from literate_ai.source_to_specification import (
    ParityOutcome,
    PromotionInputKind,
    RegenerationOutcome,
    RegenerationRunPlan,
    RegenerativeQualificationPolicy,
    SourcePromotionInput,
    canonical_digest,
    inventory_source,
    run_regenerative_qualification,
)


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


if __name__ == "__main__":
    unittest.main()
