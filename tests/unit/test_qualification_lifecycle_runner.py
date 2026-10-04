"""Lifecycle-backed qualification derives evidence instead of trusting claims."""

from __future__ import annotations

import unittest
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
    QualificationLifecycleRunner,
    QualificationParityCaseEvidence,
    QualificationParityEvidence,
    QualificationVerifierCaseMap,
    QualificationWorkspaceAllocation,
)
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
):
    acceptance = _evidence()
    component = acceptance.component_revision
    workspace = identity(f"node-workspace:{run}")
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


def _receipt(lifecycle):
    evidence = tuple(
        ProjectTestEvidence(kind, identity(f"receipt:{kind}"))
        for kind in sorted(STANDARD_FULL_REBUILD_EVIDENCE_KINDS)
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
        ProjectTestSummary(1, 1, 0, 0),
        lifecycle.identity,
        evidence,
    )


class Workspaces:
    def allocate(self, run_identity):
        return QualificationWorkspaceAllocation(
            identity(f"workspace:{run_identity.digest}"), True
        )


class LifecyclePort:
    def __init__(self, events, *, hit: bool = False, reused: bool = False):
        self.events = events
        self.hit = hit
        self.reused = reused

    def execute(self, request):
        run = request.run_identity.digest
        self.events.append(("lifecycle", run))
        lifecycle = _lifecycle(run, hit=self.hit, reused=self.reused)
        return QualificationLifecycleExecution(
            lifecycle,
            _receipt(lifecycle),
            identity(f"request:{run}"),
            identity(f"invocation:{run}"),
            identity("driver"),
            identity("policy"),
            identity("distribution"),
        )


class Verifier:
    def __init__(self, events, case_map, *, failed=()):
        self.events = events
        self.case_map = case_map
        self.failed = set(failed)

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
        cases = [
            QualificationParityCaseEvidence(
                binding.case_id,
                binding.case_identity,
                identity(f"baseline:{run_identity.digest}:{binding.case_id}"),
                identity(f"generated:{run_identity.digest}:{binding.case_id}"),
                binding.case_id not in self.failed,
            )
            for binding in case_map.cases
        ]
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


if __name__ == "__main__":
    unittest.main()
