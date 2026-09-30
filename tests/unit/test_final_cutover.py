"""Final cutover evidence and retained-reader conformance."""

from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.application import (
    FinalCutoverManifest,
    ObservationDecision,
    ObservationDisposition,
    RollbackRehearsal,
    SeamCutover,
)
from literate_ai.compatibility.ova import LifecycleState, OvaCompatibilityReader
from literate_ai.contracts import LifecycleSeam, canonical_identity

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ova"


def identity(label: str):
    return canonical_identity({"fixture": label})


def complete_manifest() -> FinalCutoverManifest:
    return FinalCutoverManifest(
        framework_release_identity=identity("literate-ai@0.1.1"),
        compatible_release_identities=(
            identity("literate-ai@0.1.0a1"),
            identity("literate-ai@0.1.1a1"),
        ),
        seams=tuple(
            SeamCutover(seam, identity(f"comparison:{seam.value}"))
            for seam in LifecycleSeam
        ),
        compatibility_reader_identity=identity("ova-compatibility-reader@1"),
        compatibility_reader_support_term=(
            "retained until an explicit major-version removal decision"
        ),
        dependency_audit_identity=identity("downstream-dependency-audit"),
        duplicate_general_lifecycle_implementation_removed=True,
        rollback=RollbackRehearsal(
            baseline_state_identity=identity("legacy-cache"),
            migrated_copy_identity=identity("migrated-cache-copy"),
            evidence_identity=identity("rollback-report"),
            prior_release_restore_verified=True,
            compatibility_reader_verified=True,
            baseline_unchanged=True,
        ),
        observation=ObservationDecision(
            ObservationDisposition.USER_DIRECTED_WITHOUT_ELAPSED_WINDOW,
            decided_by="repository-owner",
            reason="complete Phase 2 now with the unobserved risk recorded",
            directive_reference="user-directive:2026-08-02:complete-phase-2-cutover",
        ),
    )


class FinalCutoverTests(unittest.TestCase):
    def test_complete_framework_ownership_is_ready_with_visible_observation_warning(
        self,
    ) -> None:
        manifest = complete_manifest()

        self.assertTrue(manifest.ready)
        self.assertEqual(manifest.blockers, ())
        self.assertEqual(
            manifest.warnings, ("production-observation-window-not-completed",)
        )
        self.assertEqual(
            manifest.to_dict()["observation"]["disposition"],
            "user-directed-without-elapsed-window",
        )
        self.assertIsNone(manifest.to_dict()["observation"]["evidence_identity"])
        self.assertEqual(manifest.identity, canonical_identity(manifest.to_dict()))

    def test_elapsed_observation_cannot_be_claimed_without_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires evidence"):
            ObservationDecision(
                ObservationDisposition.COMPLETED,
                decided_by="release-manager",
                reason="window passed",
            )

    def test_directed_cutover_cannot_smuggle_observation_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not claim"):
            ObservationDecision(
                ObservationDisposition.USER_DIRECTED_WITHOUT_ELAPSED_WINDOW,
                decided_by="repository-owner",
                reason="cut over now",
                evidence_identity=identity("invented-window"),
                directive_reference="user-directive:cut-over-now",
            )

    def test_manifest_requires_every_seam_once_in_canonical_order(self) -> None:
        manifest = complete_manifest()
        with self.assertRaisesRegex(ValueError, "every lifecycle seam"):
            FinalCutoverManifest(
                framework_release_identity=manifest.framework_release_identity,
                compatible_release_identities=manifest.compatible_release_identities,
                seams=manifest.seams[:-1],
                compatibility_reader_identity=manifest.compatibility_reader_identity,
                compatibility_reader_support_term=(
                    manifest.compatibility_reader_support_term
                ),
                dependency_audit_identity=manifest.dependency_audit_identity,
                duplicate_general_lifecycle_implementation_removed=True,
                rollback=manifest.rollback,
                observation=manifest.observation,
            )

    def test_failed_seam_rollback_or_duplicate_audit_blocks_cutover(self) -> None:
        manifest = complete_manifest()
        failed_seams = list(manifest.seams)
        failed_seams[0] = SeamCutover(
            failed_seams[0].seam,
            failed_seams[0].comparison_evidence_identity,
            downstream_legacy_write_removed=False,
        )
        failed = FinalCutoverManifest(
            framework_release_identity=manifest.framework_release_identity,
            compatible_release_identities=manifest.compatible_release_identities,
            seams=tuple(failed_seams),
            compatibility_reader_identity=manifest.compatibility_reader_identity,
            compatibility_reader_support_term=manifest.compatibility_reader_support_term,
            dependency_audit_identity=manifest.dependency_audit_identity,
            duplicate_general_lifecycle_implementation_removed=False,
            rollback=RollbackRehearsal(
                baseline_state_identity=identity("legacy-cache"),
                migrated_copy_identity=identity("migrated-cache-copy"),
                evidence_identity=identity("failed-rollback"),
                prior_release_restore_verified=False,
                compatibility_reader_verified=True,
                baseline_unchanged=True,
            ),
            observation=manifest.observation,
        )

        self.assertFalse(failed.ready)
        self.assertEqual(
            failed.blockers,
            (
                "seam:source-identity",
                "duplicate-general-lifecycle-implementation",
                "rollback-rehearsal",
            ),
        )

    def test_cutover_keeps_all_legacy_readers_lossless_and_read_only(self) -> None:
        self.assertTrue(complete_manifest().ready)
        reader = OvaCompatibilityReader()
        valid = FIXTURES / "valid"
        cases = (
            (reader.read_component, "ova.yaml"),
            (reader.read_settings, "models.yaml"),
            (reader.read_settings, "runtime-settings.json"),
            (reader.read_cache, "cache.json"),
            (reader.read_source_package, "source-package.json"),
            (reader.read_object_package, "object-package.json"),
            (reader.read_provenance, "generation.json"),
            (reader.read_publication_settings, "publication-settings.json"),
            (reader.read_publications, "publication-records.json"),
        )
        before = {name: (valid / name).read_bytes() for _, name in cases}

        for method, name in cases:
            with self.subTest(name=name):
                self.assertEqual(method(valid / name).state, LifecycleState.VALID)

        self.assertEqual(
            {name: (valid / name).read_bytes() for _, name in cases}, before
        )


if __name__ == "__main__":
    unittest.main()
