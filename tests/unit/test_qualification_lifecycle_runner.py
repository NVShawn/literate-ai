"""Lifecycle-backed qualification derives evidence instead of trusting claims."""

from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from literate_ai.application.standard_project_lifecycle import (
    StandardNodeLifecycleResult,
    StandardProjectLifecycleResult,
)
from literate_ai.contracts import (
    PROJECT_TEST_OUTCOME_PASSED,
    PROJECT_TEST_SUITE_KIND,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestSummary,
    StandardNodeCacheDecision,
    StandardNodeCacheOutcome,
    StandardPlannedLifecycleNode,
    StandardProjectLifecycleMembership,
    VersionedContentRef,
    canonical_identity,
)
from literate_ai.contracts.executable_components import SourceGenerationDisposition
from literate_ai.contracts.standard_lifecycle_policy import (
    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
)
from literate_ai.source_to_specification.errors import SourceToSpecificationError
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationCaseSurfaceBinding,
    QualificationLifecycleExecution,
    QualificationLifecyclePlan,
    QualificationLifecycleResult,
    QualificationLifecycleRunEvidence,
    QualificationLifecycleRunner,
    QualificationParityCaseEvidence,
    QualificationParityEvidence,
    QualificationVerifierCaseMap,
    QualificationWorkspaceAllocation,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_standard_post_source_evidence import _evidence


def identity(label: str):
    return canonical_identity({"qualification-lifecycle-test": label})


class _Lifecycle(StandardProjectLifecycleResult):
    @property
    def identity(self):
        return self._fixture_identity

    @property
    def successful(self):
        return self.admission_identity is not None and self.receipt_identity is not None


def _lifecycle(
    run: str,
    *,
    hit: bool = False,
    reused: bool = False,
    node_workspace: str | None = None,
):
    acceptance = _evidence()
    component = acceptance.component_revision
    workspace = identity(node_workspace or f"node-workspace:{run}")
    source_tree = acceptance.build.source_tree_identity
    node = object.__new__(StandardNodeLifecycleResult)
    object.__setattr__(
        node,
        "source_generation",
        SimpleNamespace(
            disposition=(
                SourceGenerationDisposition.REUSED
                if reused
                else SourceGenerationDisposition.GENERATED
            )
        ),
    )
    object.__setattr__(node, "component_revision", component)
    object.__setattr__(
        node,
        "source_output",
        SimpleNamespace(
            candidate=SimpleNamespace(
                tree_identity=source_tree,
                workspace_allocation_identity=workspace,
            )
        ),
    )
    object.__setattr__(node, "index_identity", identity(f"index:{run}"))
    object.__setattr__(node, "build_evidence", acceptance.build)
    object.__setattr__(node, "generated_test_evidence", acceptance.generated_tests)
    object.__setattr__(node, "acceptance_evidence", acceptance)

    generation_plan = identity(f"generation-plan:{run}")
    generation_key = identity(f"generation-key:{run}")
    lifecycle_result = identity(f"node-result:{run}")
    accepted = identity(f"accepted-membership:{run}")
    outcome = (
        StandardNodeCacheOutcome.HIT
        if hit
        else StandardNodeCacheOutcome.FORCED_REGENERATION
    )
    decision = StandardNodeCacheDecision(
        component,
        generation_plan,
        generation_key,
        outcome,
        identity(f"input-membership:{run}") if hit else None,
        lifecycle_result,
        accepted,
        None if hit else accepted,
        None,
    )
    execution_plan = identity(f"execution-plan:{run}")
    membership = StandardProjectLifecycleMembership(
        execution_plan,
        (StandardPlannedLifecycleNode(component, generation_plan, generation_key),),
        (decision,),
    )
    aggregate = SimpleNamespace(identity=identity(f"aggregate:{run}"))
    lifecycle = object.__new__(_Lifecycle)
    for name, value in {
        "_fixture_identity": identity(f"lifecycle:{run}"),
        "execution_plan_identity": execution_plan,
        "node_results": (node,),
        "lifecycle_membership": membership,
        "admission_identity": identity(f"admission:{run}"),
        "aggregate_receipt": aggregate,
        "receipt_identity": aggregate.identity,
    }.items():
        object.__setattr__(lifecycle, name, value)
    return lifecycle


def _receipt(lifecycle, *, total: int = 1, omit_evidence: bool = False):
    kinds = sorted(STANDARD_FULL_REBUILD_EVIDENCE_KINDS)
    if omit_evidence:
        kinds.pop()
    evidence = tuple(
        ProjectTestEvidence(kind, identity(f"receipt:{kind}")) for kind in kinds
    )
    return ProjectTestReceipt(
        "fixture-project",
        identity("project-revision"),
        lifecycle.aggregate_receipt.identity,
        VersionedContentRef(
            PROJECT_TEST_SUITE_KIND,
            "standard",
            "1.0.0",
            identity("standard-policy"),
        ),
        PROJECT_TEST_OUTCOME_PASSED,
        ProjectTestSummary(total, total, 0, 0),
        lifecycle.identity,
        evidence,
    )


class Workspaces:
    def __init__(self, *, reused: bool = False, empty: bool = True):
        self.reused = reused
        self.empty = empty

    def allocate(self, run_identity):
        suffix = "shared" if self.reused else run_identity.digest
        return QualificationWorkspaceAllocation(
            identity(f"workspace:{suffix}"), self.empty
        )


class LifecyclePort:
    def __init__(
        self,
        events,
        *,
        hit: bool = False,
        reused: bool = False,
        total: int = 1,
        shared_node_workspace: bool = False,
        receipt_mismatch: bool = False,
        omit_receipt_evidence: bool = False,
        omit_node_evidence: bool = False,
        varying_binding: bool = False,
    ):
        self.events = events
        self.hit = hit
        self.reused = reused
        self.total = total
        self.shared_node_workspace = shared_node_workspace
        self.receipt_mismatch = receipt_mismatch
        self.omit_receipt_evidence = omit_receipt_evidence
        self.omit_node_evidence = omit_node_evidence
        self.varying_binding = varying_binding

    def execute(self, request):
        run = request.run_identity.digest
        self.events.append(("lifecycle", run))
        lifecycle = _lifecycle(
            run,
            hit=self.hit,
            reused=self.reused,
            node_workspace="shared" if self.shared_node_workspace else None,
        )
        if self.omit_node_evidence:
            object.__setattr__(lifecycle.node_results[0], "acceptance_evidence", None)
        receipt = _receipt(
            lifecycle,
            total=self.total,
            omit_evidence=self.omit_receipt_evidence,
        )
        if self.receipt_mismatch:
            receipt = replace(receipt, result_identity=identity("other-lifecycle"))
        return QualificationLifecycleExecution(
            lifecycle,
            receipt,
            identity(f"request:{run}"),
            identity(f"invocation:{run}"),
            identity(f"driver:{run}" if self.varying_binding else "driver"),
            identity("policy"),
            identity("distribution"),
        )


class Verifier:
    def __init__(self, events, case_map, *, failed=(), omit=(), substitute=False):
        self.events = events
        self.case_map = case_map
        self.failed = set(failed)
        self.omit = set(omit)
        self.substitute = substitute

    @property
    def provider_identity(self):
        return self.case_map.verifier_identity

    def verify(
        self,
        *,
        run_identity,
        source_snapshot_identity,
        generated_tree_identities,
        case_map,
    ):
        self.events.append(("parity", run_identity.digest))
        cases = []
        for binding in case_map.cases:
            if binding.case_id in self.omit:
                continue
            cases.append(
                QualificationParityCaseEvidence(
                    binding.case_id,
                    (
                        identity("substituted-case")
                        if self.substitute
                        else binding.case_identity
                    ),
                    identity(f"baseline:{run_identity.digest}:{binding.case_id}"),
                    identity(f"generated:{run_identity.digest}:{binding.case_id}"),
                    binding.case_id not in self.failed,
                )
            )
        return QualificationParityEvidence(
            run_identity,
            source_snapshot_identity,
            generated_tree_identities,
            self.provider_identity,
            case_map.identity,
            tuple(cases),
        )


class QualificationLifecycleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.case_map = QualificationVerifierCaseMap(
            identity("verifier"),
            (
                QualificationCaseSurfaceBinding(
                    "case-errors", identity("case-errors"), ("errors",)
                ),
                QualificationCaseSurfaceBinding(
                    "case-output", identity("case-output"), ("cli", "output")
                ),
            ),
        )
        self.plan = QualificationLifecyclePlan(
            identity("target"),
            identity("component-lock"),
            identity("specifications"),
            identity("source-snapshot"),
            identity("generation-audit"),
            identity("promotion-tree"),
            tuple(
                sorted(
                    (identity("run-1"), identity("run-2")), key=lambda item: item.uri
                )
            ),
            self.case_map,
        )

    def runner(self, *, workspaces=None, lifecycle=None, verifier=None, events=None):
        events = [] if events is None else events
        return QualificationLifecycleRunner(
            workspaces=workspaces or Workspaces(),
            lifecycle=lifecycle or LifecyclePort(events),
            parity_verifier=verifier or Verifier(events, self.case_map),
        )

    def test_two_clean_runs_derive_exact_evidence_totals_surfaces_and_order(self):
        events = []
        result = self.runner(events=events).run(self.plan)

        self.assertEqual(len(result.runs), 2)
        self.assertEqual([item.generated_test_total for item in result.runs], [1, 1])
        self.assertEqual(
            [item.covered_surface_ids for item in result.runs],
            [("cli", "errors", "output"), ("cli", "errors", "output")],
        )
        self.assertEqual(
            [item[0] for item in events],
            ["lifecycle", "parity", "lifecycle", "parity"],
        )
        self.assertEqual(
            QualificationLifecycleRunEvidence.from_dict(result.runs[0].to_dict()),
            result.runs[0],
        )
        self.assertEqual(
            QualificationLifecycleResult.from_dict(result.to_dict()), result
        )
        self.assertEqual(
            QualificationVerifierCaseMap.from_dict(self.case_map.to_dict()),
            self.case_map,
        )

    def test_failed_pinned_verifier_case_rejects_qualification(self):
        events = []
        verifier = Verifier(events, self.case_map, failed={"case-output"})
        with self.assertRaisesRegex(SourceToSpecificationError, "must pass"):
            self.runner(events=events, verifier=verifier).run(self.plan)

    def test_source_cache_hit_and_reused_source_are_rejected(self):
        for name, port in (
            ("hit", LifecyclePort([], hit=True)),
            ("reused", LifecyclePort([], reused=True)),
        ):
            with self.subTest(name=name), self.assertRaises(SourceToSpecificationError):
                self.runner(lifecycle=port).run(self.plan)

    def test_empty_or_reused_outer_and_node_workspaces_are_rejected(self):
        cases = (
            ("nonempty", Workspaces(empty=False), LifecyclePort([])),
            ("outer-reused", Workspaces(reused=True), LifecyclePort([])),
            (
                "node-reused",
                Workspaces(),
                LifecyclePort([], shared_node_workspace=True),
            ),
        )
        for name, workspaces, lifecycle in cases:
            with self.subTest(name=name), self.assertRaises(SourceToSpecificationError):
                self.runner(workspaces=workspaces, lifecycle=lifecycle).run(self.plan)

    def test_receipt_totals_are_checked_against_exact_case_membership(self):
        with self.assertRaisesRegex(SourceToSpecificationError, "total differs"):
            self.runner(lifecycle=LifecyclePort([], total=2)).run(self.plan)

    def test_substituted_or_incomplete_typed_lifecycle_evidence_is_rejected(self):
        cases = (
            ("receipt-mismatch", LifecyclePort([], receipt_mismatch=True)),
            (
                "receipt-evidence-missing",
                LifecyclePort([], omit_receipt_evidence=True),
            ),
            ("node-evidence-missing", LifecyclePort([], omit_node_evidence=True)),
        )
        for name, lifecycle in cases:
            with self.subTest(name=name), self.assertRaises(SourceToSpecificationError):
                self.runner(lifecycle=lifecycle).run(self.plan)

    def test_clean_runs_must_retain_one_exact_standard_binding(self):
        with self.assertRaisesRegex(SourceToSpecificationError, "one exact target"):
            self.runner(lifecycle=LifecyclePort([], varying_binding=True)).run(
                self.plan
            )

    def test_missing_or_substituted_parity_cases_are_rejected(self):
        for name, verifier in (
            ("missing", Verifier([], self.case_map, omit={"case-errors"})),
            ("substituted", Verifier([], self.case_map, substitute=True)),
        ):
            with self.subTest(name=name), self.assertRaises(SourceToSpecificationError):
                self.runner(verifier=verifier).run(self.plan)

    def test_verifier_must_own_the_exact_pinned_case_map(self):
        other_map = QualificationVerifierCaseMap(
            identity("other-verifier"), self.case_map.cases
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "pinned case map"):
            self.runner(verifier=Verifier([], other_map)).run(self.plan)

    def test_plan_requires_two_distinct_runs_and_canonical_contracts(self):
        with self.assertRaisesRegex(SourceToSpecificationError, "at least two"):
            QualificationLifecyclePlan(
                self.plan.target_profile_identity,
                self.plan.component_lock_identity,
                self.plan.specification_set_identity,
                self.plan.source_snapshot_identity,
                self.plan.generation_input_audit_identity,
                self.plan.promotion_tree_identity,
                (identity("only-run"),),
                self.case_map,
            )
        wire = self.case_map.to_dict()
        wire["unknown"] = True
        with self.assertRaises(SourceToSpecificationError):
            QualificationVerifierCaseMap.from_dict(wire)

    def test_case_surface_and_parity_contracts_round_trip_strictly(self):
        binding = self.case_map.cases[0]
        self.assertEqual(
            QualificationCaseSurfaceBinding.from_dict(binding.to_dict()), binding
        )
        parity = QualificationParityEvidence(
            identity("contract-run"),
            self.plan.source_snapshot_identity,
            (identity("contract-tree"),),
            self.case_map.verifier_identity,
            self.case_map.identity,
            tuple(
                QualificationParityCaseEvidence(
                    item.case_id,
                    item.case_identity,
                    identity(f"baseline:{item.case_id}"),
                    identity(f"generated:{item.case_id}"),
                    True,
                )
                for item in self.case_map.cases
            ),
        )
        self.assertEqual(
            QualificationParityEvidence.from_dict(parity.to_dict()), parity
        )
        wire = parity.to_dict()
        wire["unknown"] = True
        with self.assertRaises(SourceToSpecificationError):
            QualificationParityEvidence.from_dict(wire)

    def test_all_public_lifecycle_contracts_validate_and_reject_tampering(self):
        catalog = SchemaCatalog()
        result = self.runner().run(self.plan)
        binding = self.case_map.cases[0]
        parity_case = QualificationParityCaseEvidence(
            binding.case_id,
            binding.case_identity,
            identity("baseline-contract"),
            identity("generated-contract"),
            True,
        )
        parity = QualificationParityEvidence(
            identity("contract-run"),
            self.plan.source_snapshot_identity,
            (identity("contract-tree"),),
            self.case_map.verifier_identity,
            self.case_map.identity,
            (parity_case,),
        )
        records = (
            binding,
            self.case_map,
            parity_case,
            parity,
            result.runs[0],
            result,
        )
        for record in records:
            with self.subTest(schema=record.SCHEMA):
                wire = record.to_dict()
                catalog.validate(record.SCHEMA, wire)
                self.assertEqual(type(record).from_dict(wire), record)

                unknown = dict(wire)
                unknown["tampered"] = True
                with self.assertRaises(AssertionError):
                    catalog.validate(record.SCHEMA, unknown)
                with self.assertRaises(SourceToSpecificationError):
                    type(record).from_dict(unknown)

                wrong_schema = dict(wire)
                wrong_schema["schema"] = "urn:literate-ai:schema:v2:other"
                with self.assertRaises(AssertionError):
                    catalog.validate(record.SCHEMA, wrong_schema)
                with self.assertRaises(SourceToSpecificationError):
                    type(record).from_dict(wrong_schema)

                malformed_identity = deepcopy(wire)

                def corrupt_identity(value):
                    if isinstance(value, dict):
                        if value.get("schema") == (
                            "urn:literate-ai:schema:v1:content-identity"
                        ):
                            value["digest"] = "not-a-digest"
                            return True
                        return any(corrupt_identity(item) for item in value.values())
                    if isinstance(value, list):
                        return any(corrupt_identity(item) for item in value)
                    return False

                self.assertTrue(corrupt_identity(malformed_identity))
                with self.assertRaises(AssertionError):
                    catalog.validate(record.SCHEMA, malformed_identity)
                with self.assertRaises(SourceToSpecificationError):
                    type(record).from_dict(malformed_identity)

        failed_parity = result.to_dict()
        failed_parity["parity_evidence"][0]["cases"][0]["passed"] = False
        with self.assertRaisesRegex(SourceToSpecificationError, "parity membership"):
            QualificationLifecycleResult.from_dict(failed_parity)


if __name__ == "__main__":
    unittest.main()
