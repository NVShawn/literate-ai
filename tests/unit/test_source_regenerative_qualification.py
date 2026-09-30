from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.source_to_specification import (
    LEGACY_QUALIFICATION_BLOCKER,
    CleanRegenerationEvidence,
    RegenerativeAuthority,
    RegenerativeQualificationPolicy,
    SourceToSpecificationError,
    qualify_regenerative_specification,
)
from literate_ai.source_to_specification.contracts import canonical_digest


def identity(label: str) -> str:
    return canonical_digest({"fixture": label})


def evidence(label: str, *, target: str, surfaces: tuple[str, ...]):
    return CleanRegenerationEvidence(
        run_id=identity(f"run:{label}"),
        run_attestation_id=identity(f"attestation:{label}"),
        source_snapshot_id=identity("original-source"),
        specification_set_id=identity("accepted-specification"),
        target_profile_id=target,
        flavor_lock_id=identity(f"flavors:{label}"),
        generation_recipe_id=identity(f"recipe:{label}"),
        generated_tree_id=identity(f"tree:{label}"),
        build_result_id=identity(f"build:{label}"),
        generated_test_result_id=identity(f"test:{label}"),
        independent_parity_result_id=identity(f"parity:{label}"),
        covered_surface_ids=surfaces,
        source_excluded_from_generation=True,
        empty_workspace=True,
        generated_source_cache_hit=False,
        build_passed=True,
        generated_tests_passed=True,
        independent_parity_passed=True,
        generated_tests_total=12,
        generated_tests_succeeded=12,
        generated_tests_failed=0,
        generated_tests_skipped=0,
    )


class RegenerativeQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.macos = identity("target:macos")
        self.linux = identity("target:linux")
        self.policy = RegenerativeQualificationPolicy(
            policy_id="portable-parity@1",
            minimum_clean_runs=2,
            required_target_profile_ids=(self.linux, self.macos),
            required_surface_ids=("api", "errors", "persistence"),
        )
        self.runs = (
            evidence(
                "linux-a",
                target=self.linux,
                surfaces=("api", "errors", "persistence"),
            ),
            evidence(
                "linux-b",
                target=self.linux,
                surfaces=("api", "errors", "persistence"),
            ),
            evidence(
                "macos-a",
                target=self.macos,
                surfaces=("api", "errors", "persistence"),
            ),
            evidence(
                "macos-b",
                target=self.macos,
                surfaces=("api", "errors", "persistence"),
            ),
        )

    def qualify(self, runs=None):
        return qualify_regenerative_specification(
            source_snapshot_id=identity("original-source"),
            specification_set_id=identity("accepted-specification"),
            policy=self.policy,
            evidence=self.runs if runs is None else runs,
        )

    def test_exact_clean_legacy_evidence_remains_historical(self):
        decision = self.qualify()
        self.assertFalse(decision.qualified)
        self.assertIs(
            decision.effective_authority, RegenerativeAuthority.SOURCE_BASELINE
        )
        self.assertIs(decision.claimed_authority, RegenerativeAuthority.SOURCE_BASELINE)
        self.assertEqual(decision.blockers, (LEGACY_QUALIFICATION_BLOCKER,))

    def test_source_or_cache_input_blocks_qualification(self):
        tainted = replace(
            self.runs[0],
            source_excluded_from_generation=False,
            generated_source_cache_hit=True,
            empty_workspace=False,
        )
        decision = self.qualify((tainted, *self.runs[1:]))
        self.assertFalse(decision.qualified)
        self.assertIn("original-source-entered-generation", decision.blockers)
        self.assertIn("source-cache-used-for-qualification", decision.blockers)
        self.assertIn("nonempty-generation-workspace", decision.blockers)
        self.assertIs(
            decision.effective_authority, RegenerativeAuthority.SOURCE_BASELINE
        )

    def test_failure_skip_missing_target_and_surface_all_remain_visible(self):
        failed = tuple(
            replace(
                item,
                build_passed=False,
                generated_tests_passed=False,
                independent_parity_passed=False,
                generated_tests_succeeded=10,
                generated_tests_failed=1,
                generated_tests_skipped=1,
                covered_surface_ids=("api",),
                target_profile_id=self.macos,
            )
            for item in self.runs[:2]
        )
        decision = self.qualify((*failed, *self.runs[2:]))
        self.assertIn("build-failure", decision.blockers)
        self.assertIn("generated-test-failure", decision.blockers)
        self.assertIn("source-parity-failure", decision.blockers)
        self.assertIn("test-failure", decision.blockers)
        self.assertIn("skipped-tests", decision.blockers)
        self.assertIn(f"missing-target:{self.linux}", decision.blockers)

    def test_every_required_target_must_cover_every_required_surface(self):
        incomplete = tuple(
            replace(item, covered_surface_ids=("api", "errors"))
            for item in self.runs[2:]
        )
        decision = self.qualify((*self.runs[:2], *incomplete))
        self.assertIn(f"missing-surface:{self.macos}:persistence", decision.blockers)

    def test_minimum_clean_runs_applies_to_each_required_target(self):
        decision = self.qualify((self.runs[0], *self.runs[2:]))
        self.assertIn(
            f"clean-runs:{self.linux}:1/{self.policy.minimum_clean_runs}",
            decision.blockers,
        )

    def test_duplicate_attestation_is_not_independent_evidence(self):
        duplicate = replace(
            self.runs[1],
            run_attestation_id=self.runs[0].run_attestation_id,
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "independently unique"):
            self.qualify((self.runs[0], duplicate))

    def test_evidence_must_bind_the_exact_baseline_and_specification(self):
        other_source = replace(
            self.runs[0], source_snapshot_id=identity("other-source")
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "source snapshot"):
            self.qualify((other_source, self.runs[1]))
        other_specification = replace(
            self.runs[0], specification_set_id=identity("other-specification")
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "specification set"):
            self.qualify((other_specification, self.runs[1]))

    def test_minimum_clean_runs_must_be_an_integer(self):
        with self.assertRaisesRegex(SourceToSpecificationError, "at least two"):
            RegenerativeQualificationPolicy(
                policy_id="invalid@1",
                minimum_clean_runs="2",  # type: ignore[arg-type]
                required_target_profile_ids=(),
                required_surface_ids=(),
            )
        with self.assertRaisesRegex(SourceToSpecificationError, "at least two"):
            RegenerativeQualificationPolicy(
                policy_id="invalid@1",
                minimum_clean_runs=1,
                required_target_profile_ids=(),
                required_surface_ids=(),
            )

    def test_policy_rejects_empty_required_target_profiles(self):
        with self.assertRaisesRegex(
            SourceToSpecificationError,
            "required_target_profile_ids must contain at least one target",
        ):
            RegenerativeQualificationPolicy(
                policy_id="invalid@1",
                minimum_clean_runs=2,
                required_target_profile_ids=(),
                required_surface_ids=("api",),
            )

    def test_policy_rejects_empty_required_surfaces(self):
        with self.assertRaisesRegex(
            SourceToSpecificationError,
            "required_surface_ids must contain at least one surface",
        ):
            RegenerativeQualificationPolicy(
                policy_id="invalid@1",
                minimum_clean_runs=2,
                required_target_profile_ids=(self.linux,),
                required_surface_ids=(),
            )

    def test_public_wire_records_round_trip_strictly(self):
        decision = self.qualify()
        for record in (self.policy, self.runs[0], decision):
            with self.subTest(record=type(record).__name__):
                self.assertEqual(type(record).from_dict(record.to_dict()), record)
                self.assertTrue(record.identity.startswith("sha256:"))

    def test_wire_decoder_rejects_wrong_schema_and_unknown_fields(self):
        value = self.policy.to_dict()
        value["schema"] = "urn:literate-ai:schema:v2:invented"
        with self.assertRaisesRegex(SourceToSpecificationError, "schema must be"):
            RegenerativeQualificationPolicy.from_dict(value)
        value = self.policy.to_dict()
        value["untrusted_override"] = True
        with self.assertRaisesRegex(SourceToSpecificationError, "unknown"):
            RegenerativeQualificationPolicy.from_dict(value)

        decision_value = self.qualify().to_dict()
        decision_value["evidence_ids"] = decision_value["evidence_ids"][:1]
        decision_value["claimed_authority"] = "specification"
        decision_value["effective_authority"] = "source-baseline"
        decision_value["blockers"] = []
        with self.assertRaisesRegex(SourceToSpecificationError, "at least two"):
            type(self.qualify()).from_dict(decision_value)

        forged_effective = self.qualify().to_dict()
        forged_effective["effective_authority"] = "specification"
        with self.assertRaisesRegex(
            SourceToSpecificationError, "must remain source-baseline"
        ):
            type(self.qualify()).from_dict(forged_effective)

    def test_skips_are_allowed_only_when_policy_says_so(self):
        skipped = tuple(
            replace(
                item,
                generated_tests_succeeded=11,
                generated_tests_skipped=1,
            )
            for item in self.runs
        )
        blocked = self.qualify(skipped)
        self.assertIn("skipped-tests", blocked.blockers)
        allowed = replace(self.policy, allow_skipped_tests=True)
        decision = qualify_regenerative_specification(
            source_snapshot_id=identity("original-source"),
            specification_set_id=identity("accepted-specification"),
            policy=allowed,
            evidence=skipped,
        )
        self.assertFalse(decision.qualified)
        self.assertIs(
            decision.effective_authority, RegenerativeAuthority.SOURCE_BASELINE
        )
        self.assertNotIn("skipped-tests", decision.blockers)
        self.assertIn(LEGACY_QUALIFICATION_BLOCKER, decision.blockers)


if __name__ == "__main__":
    unittest.main()
