"""Authored records around real Standard composition with simulated host ports.

The Standard service, typed generation/stage records and diamond membership are
real. Generation, native commands, oracles, review and installed framework pins
are fixtures: these controls do not qualify a model run or retained admission.
"""

import unittest
from dataclasses import replace

from literate_ai.contracts.authored_qualification_acceptance import (
    AuthoredNodeIndependentAcceptance,
)
from literate_ai.contracts.authored_qualification_comparisons import (
    AuthoredQualificationComparison,
)
from literate_ai.contracts.authored_qualification_plans import (
    AuthoredQualificationPlan,
    AuthoredQualificationProjectReview,
)
from literate_ai.contracts.authored_qualification_requests import (
    AuthoredQualificationLifecycleRequest,
)
from literate_ai.contracts.authored_qualification_runs import (
    AuthoredQualificationNodeRun,
    AuthoredQualificationResult,
    AuthoredQualificationRun,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.qualification_origins import AuthoredQualificationOrigin
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleExecution,
)
from tests.support import fixtures_test_qualification_lifecycle_runner as receipts
from tests.support import fixtures_test_standard_project_lifecycle as standard
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def identity(label):
    return canonical_identity({"authored-run-fixture": label})


class AuthoredQualificationRunTests(unittest.TestCase):
    def setUp(self):
        f = standard.StandardProjectLifecycleTests("runTest")
        f.setUp()
        self.fixture = f
        origin = AuthoredQualificationOrigin(
            identity("project"),
            identity("locked-authority"),
            f.lock.identity,
            identity("authored-inputs"),
        )
        self.plan = AuthoredQualificationPlan(
            origin,
            (
                AuthoredQualificationProjectReview(
                    identity("project"), identity("review")
                ),
            ),
            identity("profile"),
            identity("oracles"),
            f.execution,
            identity("coding-provider"),
            identity("driver"),
            identity("policy"),
            identity("distribution"),
            "litai/qualification",
            2,
        )
        self.executions = {}
        self.ports = {}
        self.runs = (self.run_fixture("one"), self.run_fixture("two"))

    def run_fixture(self, label):
        f = self.fixture
        nodes = {
            key: replace(
                value,
                workspace=replace(
                    value.workspace,
                    allocation_identity=identity(label + key),
                ),
            )
            for key, value in f.nodes.items()
        }
        ports = standard.ContractEvidenceLifecyclePorts(
            f.execution,
            f.names,
            library_exports=getattr(self, "library_exports", False),
        )
        if getattr(self, "library_exports", False):
            ports.artifact_target_identity = f.lock.target_profile_identity
        self.ports[label] = ports
        lifecycle = standard._service(ports).execute(
            f.execution,
            component_lock=f.lock,
            invalidation=standard._decision(
                f.execution, f.names, "money", tuple(f.names.values())
            ),
            prepared_nodes=nodes,
            max_parallelism=2,
        )
        self.assertTrue(lifecycle.successful)
        records = tuple(
            AuthoredQualificationNodeRun(
                n.source_generation,
                n.source_output,
                n.index_identity,
                n.acceptance_evidence,
                n.identity,
            )
            for n in sorted(
                lifecycle.node_results, key=lambda n: n.component_revision.uri
            )
        )
        request = AuthoredQualificationLifecycleRequest(
            self.plan.origin,
            self.plan.identity,
            self.plan.oracle_set_identity,
            getattr(self, "run_identities", {}).get(label, identity("run-" + label)),
            getattr(self, "allocation_identities", {}).get(
                label, identity("allocation-" + label)
            ),
        )
        comparison = AuthoredQualificationComparison(
            self.plan.origin,
            self.plan.identity,
            request.run_identity,
            self.plan.oracle_set_identity,
            lifecycle.root_integration.independent_acceptance_identity,
            tuple(
                AuthoredNodeIndependentAcceptance(
                    n.component_revision,
                    identity("oracle-" + n.component_revision.uri),
                    identity("cases-" + n.component_revision.uri),
                    identity("observation-" + label + n.component_revision.uri),
                    1,
                )
                for n in records
            ),
        )
        receipt = receipts._receipt(
            lifecycle, total=sum(n.generated_test_total for n in records)
        )
        self.executions[label] = QualificationLifecycleExecution(
            lifecycle,
            receipt,
            identity("request-" + label),
            identity("invocation-" + label),
            self.plan.driver_identity,
            self.plan.lifecycle_policy_identity,
            self.plan.framework_distribution_identity,
        )
        return AuthoredQualificationRun(
            request,
            self.plan.driver_identity,
            self.plan.lifecycle_policy_identity,
            self.plan.framework_distribution_identity,
            identity("request-" + label),
            identity("invocation-" + label),
            lifecycle.identity,
            receipt,
            lifecycle.root_integration,
            comparison,
            records,
        )

    def test_two_complete_diamond_runs_round_trip_with_actual_node_attribution(self):
        result = AuthoredQualificationResult(self.plan, self.runs)
        self.assertEqual(
            AuthoredQualificationResult.from_dict(result.to_dict()), result
        )
        catalog = SchemaCatalog()
        catalog.validate(result.SCHEMA, result.to_dict())
        self.assertEqual([run.generated_test_total for run in result.runs], [4, 4])
        self.assertEqual([len(run.nodes) for run in result.runs], [4, 4])
        self.assertNotEqual(result.runs[0].identity, result.runs[1].identity)

    def test_source_history_and_aggregate_count_claims_are_not_wire_fields(self):
        result = AuthoredQualificationResult(self.plan, self.runs)
        for name in (
            "source_snapshot_identity",
            "promotion_tree_identity",
            "generated_test_total",
        ):
            wire = {**result.to_dict(), name: identity("forged").to_dict()}
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    AuthoredQualificationResult.from_dict(wire)
                with self.assertRaises(AssertionError):
                    SchemaCatalog().validate(result.SCHEMA, wire)

    def test_one_run_duplicate_run_and_reused_native_result_refuse(self):
        one, two = self.runs
        for runs in (
            (one,),
            (one, one),
            (
                one,
                replace(
                    two,
                    lifecycle_result_identity=one.lifecycle_result_identity,
                    receipt=replace(
                        two.receipt, result_identity=one.lifecycle_result_identity
                    ),
                ),
            ),
        ):
            with self.subTest(runs=len(runs)):
                with self.assertRaises(ValueError):
                    AuthoredQualificationResult(self.plan, runs)

    def test_shared_standard_invocation_intent_keeps_distinct_real_node_results(self):
        one, two = self.runs
        two = replace(
            two,
            lifecycle_request_identity=one.lifecycle_request_identity,
            lifecycle_invocation_identity=one.lifecycle_invocation_identity,
        )
        result = AuthoredQualificationResult(self.plan, (one, two))
        self.assertNotEqual(
            one.lifecycle_result_identity, two.lifecycle_result_identity
        )
        self.assertNotEqual(one.receipt.identity, two.receipt.identity)
        self.assertEqual(len(result.runs), 2)
        self.assertEqual([r.generated_test_total for r in result.runs], [4, 4])

    def test_borrowed_workspace_or_altered_plan_cannot_establish_clean_second_run(self):
        one, two = self.runs
        borrowed = replace(
            two,
            request=replace(
                two.request,
                workspace_allocation_identity=one.request.workspace_allocation_identity,
            ),
        )
        with self.assertRaisesRegex(ValueError, "distinct"):
            AuthoredQualificationResult(self.plan, (one, borrowed))
        with self.assertRaises(ValueError):
            AuthoredQualificationResult(
                replace(self.plan, framework_distribution_identity=identity("changed")),
                self.runs,
            )

    def test_consistent_root_only_records_still_refuse_the_complete_graph_plan(self):
        one, two = self.runs
        node = two.nodes[0]
        comparison = replace(
            two.comparison,
            nodes=tuple(
                n
                for n in two.comparison.nodes
                if n.component_revision_identity == node.component_revision
            ),
        )
        root_only = replace(
            two,
            nodes=(node,),
            comparison=comparison,
            receipt=replace(
                two.receipt,
                summary=replace(two.receipt.summary, total=1, passed=1),
            ),
        )
        with self.assertRaisesRegex(ValueError, "complete plan"):
            AuthoredQualificationResult(self.plan, (one, root_only))

    def test_original_model_output_cannot_be_replaced_with_retained_source(self):
        node = self.runs[0].nodes[0]
        provenance = replace(
            node.source_output.provenance,
            route_decision_identities=(),
            model_stage_output_identities=(),
            retained_source_identity=identity("retained-source"),
        )
        output = replace(
            node.source_output,
            provenance=provenance,
            provenance_identity=provenance.identity,
        )
        generation = replace(
            node.source_generation, provenance_identity=provenance.identity
        )
        with self.assertRaisesRegex(ValueError, "fresh model"):
            replace(node, source_generation=generation, source_output=output)

    def test_shared_case_blob_counts_every_real_node_execution(self):
        run = self.runs[0]
        shared = identity("same-content-addressed-raw-case")
        nodes = []
        for node in run.nodes:
            tests = node.acceptance.generated_tests
            tests = replace(
                tests,
                selected_case_identities=(shared,),
                cases=(replace(tests.cases[0], case_identity=shared),),
            )
            nodes.append(
                replace(
                    node, acceptance=replace(node.acceptance, generated_tests=tests)
                )
            )
        counted = replace(run, nodes=tuple(nodes))
        self.assertEqual(counted.generated_test_total, 4)
        self.assertEqual(
            len(
                {
                    c.case_identity
                    for n in counted.nodes
                    for c in n.acceptance.generated_tests.cases
                }
            ),
            1,
        )
        with self.assertRaisesRegex(ValueError, "complete run"):
            replace(
                counted,
                receipt=replace(
                    counted.receipt,
                    summary=replace(counted.receipt.summary, total=1, passed=1),
                ),
            )

    def test_foreign_build_and_substituted_generation_plan_refuse(self):
        node, foreign = self.runs[0].nodes[:2]
        with self.assertRaises(ValueError):
            replace(node, acceptance=foreign.acceptance)
        changed = replace(
            node.source_generation, generation_plan_identity=identity("foreign-plan")
        )
        with self.assertRaises(ValueError):
            replace(node, source_generation=changed)
