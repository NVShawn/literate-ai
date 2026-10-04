"""Exact Standard plan/cache/result/receipt membership tests."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.application import (
    StandardLifecycleMembershipError,
    validate_standard_aggregate_receipt,
)
from literate_ai.contracts import (
    StandardAggregateReceipt,
    StandardNodeCacheDecision,
    StandardNodeCacheOutcome,
    StandardNodeFailureEvidence,
    StandardNodeFailurePhase,
    StandardPlannedLifecycleNode,
    StandardProjectLifecycleMembership,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_standard_project_lifecycle import _identity


def _planned(name: str) -> StandardPlannedLifecycleNode:
    return StandardPlannedLifecycleNode(
        _identity(f"revision-{name}"),
        _identity(f"plan-{name}"),
        _identity(f"key-{name}"),
    )


def _decision(
    node: StandardPlannedLifecycleNode,
    *,
    outcome: StandardNodeCacheOutcome = StandardNodeCacheOutcome.MISS,
) -> StandardNodeCacheDecision:
    input_membership = (
        _identity(f"input-{node.component_revision.uri}")
        if outcome is StandardNodeCacheOutcome.HIT
        else None
    )
    accepted = _identity(f"accepted-{node.component_revision.uri}")
    publication = accepted if outcome is not StandardNodeCacheOutcome.HIT else None
    return StandardNodeCacheDecision(
        node.component_revision,
        node.generation_plan_identity,
        node.generation_key_identity,
        outcome,
        input_membership,
        _identity(f"result-{node.component_revision.uri}"),
        accepted,
        publication,
    )


class StandardLifecycleMembershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.nodes = tuple(
            sorted(
                (_planned("a"), _planned("b")), key=lambda x: x.component_revision.uri
            )
        )
        self.decisions = tuple(_decision(item) for item in self.nodes)
        self.membership = StandardProjectLifecycleMembership(
            _identity("execution-plan"), self.nodes, self.decisions
        )
        self.receipt = StandardAggregateReceipt(
            self.membership.execution_plan_identity,
            self.membership.identity,
            self.membership.lifecycle_result_identities,
            _identity("admission"),
            tuple(_identity(f"journal-{index}") for index in range(len(self.nodes))),
            tuple(_identity(f"benchmark-{index}") for index in range(len(self.nodes))),
            _identity("context-cache-report"),
        )
        self.schemas = SchemaCatalog()

    def test_all_documents_round_trip_and_validate(self) -> None:
        failure = StandardNodeFailureEvidence(
            self.nodes[0].component_revision,
            StandardNodeFailurePhase.BUILD,
            "builder.toolchain-unavailable",
            self.nodes[0].generation_plan_identity,
        )
        documents = (
            failure,
            *self.nodes,
            *self.decisions,
            self.membership,
            self.receipt,
        )
        for document in documents:
            with self.subTest(schema=document.SCHEMA):
                self.schemas.validate(document.SCHEMA, document.to_dict())
                decoded = type(document).from_dict(document.to_dict())
                self.assertEqual(decoded, document)
                self.assertEqual(decoded.identity, document.identity)

    def test_omitted_and_extra_cache_decisions_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact canonical membership"):
            StandardProjectLifecycleMembership(
                self.membership.execution_plan_identity,
                self.nodes,
                self.decisions[:-1],
            )
        extra_node = _planned("c")
        with self.assertRaisesRegex(ValueError, "exact canonical membership"):
            StandardProjectLifecycleMembership(
                self.membership.execution_plan_identity,
                self.nodes,
                (*self.decisions, _decision(extra_node)),
            )

    def test_substituted_plan_or_lifecycle_result_fails_closed(self) -> None:
        substituted_plan = replace(
            self.decisions[0], generation_key_identity=_identity("substituted-key")
        )
        with self.assertRaisesRegex(ValueError, "planned node"):
            StandardProjectLifecycleMembership(
                self.membership.execution_plan_identity,
                self.nodes,
                (substituted_plan, *self.decisions[1:]),
            )
        duplicated_result = replace(
            self.decisions[1],
            lifecycle_result_identity=self.decisions[0].lifecycle_result_identity,
        )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            StandardProjectLifecycleMembership(
                self.membership.execution_plan_identity,
                self.nodes,
                (self.decisions[0], duplicated_result),
            )

    def test_hit_cannot_claim_publication_and_miss_may_remain_staged(self) -> None:
        hit = _decision(self.nodes[0], outcome=StandardNodeCacheOutcome.HIT)
        with self.assertRaisesRegex(ValueError, "accepted non-hit"):
            replace(hit, publication_identity=hit.accepted_membership_identity)
        miss = _decision(self.nodes[0])
        staged = replace(miss, publication_identity=None)
        self.assertEqual(staged.accepted_membership_identity, miss.publication_identity)
        self.assertIsNone(staged.publication_identity)

    def test_failure_evidence_is_portable_and_excludes_success_claims(self) -> None:
        failure = StandardNodeFailureEvidence(
            self.nodes[0].component_revision,
            StandardNodeFailurePhase.TEST,
            "test.timeout",
            self.nodes[0].generation_plan_identity,
        )
        self.assertEqual(
            StandardNodeFailureEvidence.from_dict(failure.to_dict()), failure
        )
        with self.assertRaisesRegex(ValueError, "portable code"):
            replace(failure, code="token=x /private/source")
        with self.assertRaisesRegex(ValueError, "cannot claim acceptance"):
            replace(self.decisions[0], failure_identity=failure.identity)

    def test_receipt_validation_rejects_omitted_extra_and_tampered_membership(
        self,
    ) -> None:
        execution_plan = type(
            "ExecutionPlanEvidence",
            (),
            {"identity": self.membership.execution_plan_identity},
        )()
        # The validation helper intentionally performs only identity comparisons, so a
        # minimal typed-plan stand-in is sufficient for these negative cases.
        for receipt in (
            replace(
                self.receipt,
                lifecycle_result_identities=(
                    self.receipt.lifecycle_result_identities[:-1]
                ),
                context_prompt_journal_identities=(
                    self.receipt.context_prompt_journal_identities[:-1]
                ),
                context_benchmark_record_identities=(
                    self.receipt.context_benchmark_record_identities[:-1]
                ),
            ),
            replace(
                self.receipt,
                lifecycle_result_identities=(
                    *self.receipt.lifecycle_result_identities,
                    _identity("extra-result"),
                ),
                context_prompt_journal_identities=(
                    *self.receipt.context_prompt_journal_identities,
                    _identity("extra-journal"),
                ),
                context_benchmark_record_identities=(
                    *self.receipt.context_benchmark_record_identities,
                    _identity("extra-benchmark"),
                ),
            ),
            replace(
                self.receipt,
                lifecycle_membership_identity=_identity("tampered-membership"),
            ),
        ):
            with self.subTest(receipt=receipt.identity.uri):
                with self.assertRaises(StandardLifecycleMembershipError):
                    validate_standard_aggregate_receipt(
                        receipt,
                        execution_plan,
                        self.membership,
                        self.receipt.admission_identity,
                        self.receipt.context_prompt_journal_identities,
                        self.receipt.context_benchmark_record_identities,
                        self.receipt.context_cache_report_identity,
                    )

    def test_receipt_retains_exact_root_integration_identity(self) -> None:
        root_integration = _identity("root-integration")
        receipt = replace(
            self.receipt,
            root_integration_evidence_identity=root_integration,
        )
        self.schemas.validate(receipt.SCHEMA, receipt.to_dict())
        self.assertEqual(StandardAggregateReceipt.from_dict(receipt.to_dict()), receipt)
        self.assertNotEqual(receipt.identity, self.receipt.identity)

        execution_plan = type(
            "ExecutionPlanEvidence",
            (),
            {"identity": self.membership.execution_plan_identity},
        )()
        with self.assertRaises(StandardLifecycleMembershipError):
            validate_standard_aggregate_receipt(
                receipt,
                execution_plan,
                self.membership,
                receipt.admission_identity,
                receipt.context_prompt_journal_identities,
                receipt.context_benchmark_record_identities,
                receipt.context_cache_report_identity,
                _identity("substituted-root-integration"),
            )

    def test_receipt_rejects_missing_or_substituted_context_custody(self) -> None:
        with self.assertRaisesRegex(ValueError, "cover every exact lifecycle result"):
            replace(self.receipt, context_prompt_journal_identities=())
        execution_plan = type(
            "ExecutionPlanEvidence",
            (),
            {"identity": self.membership.execution_plan_identity},
        )()
        with self.assertRaises(StandardLifecycleMembershipError):
            validate_standard_aggregate_receipt(
                replace(
                    self.receipt,
                    context_cache_report_identity=_identity("substituted-report"),
                ),
                execution_plan,
                self.membership,
                self.receipt.admission_identity,
                self.receipt.context_prompt_journal_identities,
                self.receipt.context_benchmark_record_identities,
                self.receipt.context_cache_report_identity,
            )


if __name__ == "__main__":
    unittest.main()
