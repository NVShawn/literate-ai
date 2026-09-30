"""Strict domain tests for canonical project test receipts."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.contracts import (
    ContractValidationError,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
)


def identity(label: str):
    return canonical_identity({"fixture": label})


def receipt() -> ProjectTestReceipt:
    return ProjectTestReceipt(
        project_id="example-project",
        project_revision_identity=identity("project-authority"),
        subject_identity=identity("test-input-closure"),
        suite=VersionedContentRef(
            "test-suite",
            "portable-e2e",
            "1.0.0",
            identity("suite"),
        ),
        outcome="passed",
        summary=ProjectTestSummary(total=3, passed=3, failed=0, skipped=0),
        result_identity=identity("normalized-test-result"),
        evidence=(
            ProjectTestEvidence("build-result", identity("build")),
            ProjectTestEvidence("lifecycle-command", identity("lifecycle-command")),
            ProjectTestEvidence("lifecycle-request", identity("lifecycle-request")),
            ProjectTestEvidence("observation-result", identity("runtime")),
            ProjectTestEvidence(
                "source-cache-decision", identity("source-cache-decision")
            ),
            ProjectTestEvidence(
                "source-cache-lifecycle", identity("source-cache-lifecycle")
            ),
        ),
    )


def policy() -> ProjectTestReceiptPolicy:
    return ProjectTestReceiptPolicy(
        suite_id="portable-e2e",
        suite_version="1.0.0",
        runner_identity=identity("trusted-runner"),
        required_evidence_kinds=("build-result", "test-report", "test-runner"),
        minimum_test_count=3,
    )


class ProjectTestReceiptTests(unittest.TestCase):
    def test_provisional_receipt_binds_exact_outer_finalization_context(self) -> None:
        value = receipt()
        provisional = ProjectTestReceiptProvisional(
            lifecycle_request_identity=identity("lifecycle-request"),
            lifecycle_command_identity=identity("lifecycle-command"),
            source_cache_control_identity=identity("source-cache-control"),
            component_lock_identities=(identity("component-lock"),),
            receipt_identity=value.identity,
            receipt=value,
        )

        rebuilt = ProjectTestReceiptProvisional.from_dict(provisional.to_dict())

        self.assertEqual(rebuilt, provisional)
        self.assertEqual(rebuilt.identity, provisional.identity)
        rebuilt.validate_finalization_context(
            lifecycle_request_identity=identity("lifecycle-request"),
            lifecycle_command_identity=identity("lifecycle-command"),
            source_cache_control_identity=identity("source-cache-control"),
            component_lock_identities=(identity("component-lock"),),
        )
        with self.assertRaisesRegex(ContractValidationError, "outer CLI"):
            rebuilt.validate_finalization_context(
                lifecycle_request_identity=identity("another-request"),
                lifecycle_command_identity=identity("lifecycle-command"),
                source_cache_control_identity=identity("source-cache-control"),
                component_lock_identities=(identity("component-lock"),),
            )
        with self.assertRaisesRegex(ContractValidationError, "exact planned"):
            rebuilt.validate_finalization_context(
                lifecycle_request_identity=identity("lifecycle-request"),
                lifecycle_command_identity=identity("lifecycle-command"),
                source_cache_control_identity=identity("source-cache-control"),
                component_lock_identities=(identity("substituted-component-lock"),),
            )
        with self.assertRaisesRegex(ContractValidationError, "nested receipt"):
            replace(provisional, receipt_identity=identity("another-receipt"))

    def test_policy_round_trips_with_stable_identity(self) -> None:
        value = policy()

        rebuilt = ProjectTestReceiptPolicy.from_dict(value.to_dict())

        self.assertEqual(rebuilt, value)
        self.assertEqual(rebuilt.identity, value.identity)

    def test_finalized_candidate_preserves_raw_receipt_and_outer_bindings(self) -> None:
        value = receipt()
        provisional = ProjectTestReceiptProvisional(
            lifecycle_request_identity=identity("lifecycle-request"),
            lifecycle_command_identity=identity("lifecycle-command"),
            source_cache_control_identity=identity("source-cache-control"),
            component_lock_identities=(identity("component-lock"),),
            receipt_identity=value.identity,
            receipt=value,
        )
        candidate = ProjectTestReceiptFinalizedCandidate.finalize(
            provisional,
            source_cache_decision_identity=identity("source-cache-decision"),
            source_cache_lifecycle_identity=identity("source-cache-lifecycle"),
        )

        rebuilt = ProjectTestReceiptFinalizedCandidate.from_dict(candidate.to_dict())

        self.assertEqual(rebuilt, candidate)
        self.assertEqual(rebuilt.receipt, value)
        self.assertEqual(rebuilt.receipt_identity, value.identity)
        self.assertEqual(
            rebuilt.component_lock_identities,
            provisional.component_lock_identities,
        )
        self.assertEqual(
            rebuilt.to_dict()["finalization_boundary"], "supported-api-tcb"
        )

    def test_finalized_candidate_rejects_each_tampered_outer_binding(self) -> None:
        value = receipt()
        provisional = ProjectTestReceiptProvisional(
            lifecycle_request_identity=identity("lifecycle-request"),
            lifecycle_command_identity=identity("lifecycle-command"),
            source_cache_control_identity=identity("source-cache-control"),
            component_lock_identities=(identity("component-lock"),),
            receipt_identity=value.identity,
            receipt=value,
        )
        candidate = ProjectTestReceiptFinalizedCandidate.finalize(
            provisional,
            source_cache_decision_identity=identity("source-cache-decision"),
            source_cache_lifecycle_identity=identity("source-cache-lifecycle"),
        )

        for field in (
            "provisional_identity",
            "lifecycle_request_identity",
            "lifecycle_command_identity",
            "source_cache_control_identity",
            "source_cache_decision_identity",
            "source_cache_lifecycle_identity",
        ):
            with self.subTest(field=field):
                with self.assertRaises(ContractValidationError):
                    replace(candidate, **{field: identity(f"tampered-{field}")})
        with self.assertRaises(ContractValidationError):
            replace(
                candidate,
                component_lock_identities=(identity("tampered-component-lock"),),
            )

    def test_policy_requires_a_canonical_explicit_runner_admission(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "must be sorted"):
            replace(
                policy(),
                required_evidence_kinds=(
                    "test-runner",
                    "build-result",
                    "test-report",
                ),
            )
        with self.assertRaisesRegex(ContractValidationError, "must include"):
            replace(
                policy(),
                required_evidence_kinds=("build-result", "test-report"),
            )
        with self.assertRaisesRegex(ContractValidationError, "unsupported"):
            replace(
                policy(),
                required_evidence_kinds=("stdout", "test-runner"),
            )
        with self.assertRaisesRegex(ContractValidationError, "between 1"):
            replace(policy(), minimum_test_count=0)
        with self.assertRaisesRegex(ContractValidationError, "ContentIdentity"):
            replace(policy(), runner_identity="sha256:" + "0" * 64)

    def test_passing_receipt_round_trips_with_stable_identity(self) -> None:
        value = receipt()

        rebuilt = ProjectTestReceipt.from_dict(value.to_dict())

        self.assertEqual(rebuilt, value)
        self.assertEqual(rebuilt.identity, value.identity)
        self.assertNotIn("started_at", rebuilt.to_dict())
        self.assertEqual(value.to_dict()["tests"], 3)
        self.assertNotIn("outcome", value.to_dict())
        self.assertNotIn("summary", value.to_dict())
        self.assertIsInstance(value.to_dict()["project_revision"], str)

    def test_summary_requires_a_nonempty_wholly_passing_suite(self) -> None:
        invalid_counts = (
            (0, 0, 0, 0),
            (3, 2, 1, 0),
            (3, 2, 0, 1),
            (3, 2, 0, 0),
        )
        for total, passed, failed, skipped in invalid_counts:
            with self.subTest(counts=(total, passed, failed, skipped)):
                with self.assertRaises(ContractValidationError):
                    ProjectTestSummary(total, passed, failed, skipped)
        with self.assertRaisesRegex(ContractValidationError, "must be an integer"):
            ProjectTestSummary(True, 1, 0, 0)

    def test_receipt_rejects_nonpassing_outcomes_and_wrong_suite_kinds(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "must be 'passed'"):
            replace(receipt(), outcome="failed")

        with self.assertRaisesRegex(ContractValidationError, "test-suite"):
            replace(
                receipt(),
                suite=VersionedContentRef(
                    "workflow", "portable-e2e", "1.0.0", identity("suite")
                ),
            )

    def test_evidence_is_bounded_typed_unique_and_canonically_ordered(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "must be one of"):
            ProjectTestEvidence("stdout", identity("stdout"))

        first, second = receipt().evidence[:2]
        with self.assertRaisesRegex(ContractValidationError, "sorted"):
            ProjectTestReceipt(
                project_id="example-project",
                project_revision_identity=identity("project-authority"),
                subject_identity=identity("test-input-closure"),
                suite=VersionedContentRef(
                    "test-suite", "portable-e2e", "1.0.0", identity("suite")
                ),
                outcome="passed",
                summary=ProjectTestSummary(1, 1, 0, 0),
                result_identity=identity("result"),
                evidence=(second, first),
            )

        with self.assertRaisesRegex(ContractValidationError, "at most once"):
            replace(receipt(), evidence=(first, first))

        too_many = tuple(
            sorted(
                (
                    ProjectTestEvidence("test-report", identity(f"report-{index}"))
                    for index in range(33)
                ),
                key=lambda item: (item.kind, item.identity.uri),
            )
        )
        with self.assertRaisesRegex(ContractValidationError, "at most 32"):
            replace(receipt(), evidence=too_many)

    def test_wire_shapes_are_closed(self) -> None:
        value = receipt().to_dict()
        value["duration_seconds"] = 1
        with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
            ProjectTestReceipt.from_dict(value)

        value = receipt().to_dict()
        value["suite"]["duration_seconds"] = 1
        with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
            ProjectTestReceipt.from_dict(value)


if __name__ == "__main__":
    unittest.main()
