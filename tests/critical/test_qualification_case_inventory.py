"""Contract-only multi-Component case inventory/count guards.

The existing lifecycle fixtures supply inert typed evidence. Actual native
all-node qualification and closed replay are separately required controls.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.contracts import StandardProjectLifecycleMembership, canonical_identity
from literate_ai.source_to_specification.errors import SourceToSpecificationError
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleExecution,
    QualificationLifecycleResult,
)
from tests.support import (
    fixtures_test_qualification_lifecycle_runner as lifecycle_fixture,
)
from tests.support import (
    fixtures_test_standard_post_source_evidence as evidence_fixture,
)


def execution(run, *, shared=True, missing=False, duplicated=False, receipt_total=9):
    """Use distinct Component/suite/observation identities and shared case bytes."""
    pieces = []
    evidence_identity = evidence_fixture._identity
    lifecycle_identity = lifecycle_fixture.identity
    for component in ["alpha", "beta", "gamma"]:

        def local_evidence(label, component=component):
            return evidence_identity(component + ":" + label)

        def local_lifecycle(label, component=component):
            return lifecycle_identity(
                label
                if label.startswith("execution-plan:")
                else component + ":" + label
            )

        with (
            patch.object(evidence_fixture, "_identity", side_effect=local_evidence),
            patch.object(lifecycle_fixture, "identity", side_effect=local_lifecycle),
        ):
            item = lifecycle_fixture._lifecycle(run)
        node = item.node_results[0]
        old = node.generated_test_evidence
        cases = []
        for value in [0, 37, 4294967295]:
            content = {
                "schema": "literate-ai/generated-test-case@1",
                "case_id": "value-" + str(value),
                "category": "example",
                "specification_refs": ["component.md" if shared else component + ".md"],
                "arguments": [value],
                "expected_result": value,
            }
            cases.append(
                replace(
                    old.cases[0],
                    case_id=content["case_id"],
                    case_identity=canonical_identity(content),
                    observation_identity=canonical_identity(
                        {"component": component, "run": run, "observed_case": value}
                    ),
                )
            )
        cases = tuple(sorted(cases, key=lambda row: row.case_identity.uri))
        tests = replace(
            old,
            selected_case_identities=tuple(row.case_identity for row in cases),
            cases=cases,
            selected_count=3,
            executed_count=3,
            passed_count=3,
        )
        acceptance = replace(node.acceptance_evidence, generated_tests=tests)
        object.__setattr__(node, "generated_test_evidence", tests)
        object.__setattr__(node, "acceptance_evidence", acceptance)
        pieces.append(item)
    pieces.sort(key=lambda item: item.node_results[0].component_revision.uri)
    composite = pieces[0]
    nodes = tuple(item.node_results[0] for item in pieces)
    membership = StandardProjectLifecycleMembership(
        composite.execution_plan_identity,
        tuple(item.lifecycle_membership.planned_nodes[0] for item in pieces),
        tuple(item.lifecycle_membership.cache_decisions[0] for item in pieces),
    )
    object.__setattr__(
        composite, "node_results", (*nodes, nodes[0]) if duplicated else nodes
    )
    object.__setattr__(composite, "lifecycle_membership", membership)
    if missing:
        object.__setattr__(nodes[0], "generated_test_evidence", None)
    receipt = lifecycle_fixture._receipt(composite, total=receipt_total)
    return QualificationLifecycleExecution(
        composite,
        receipt,
        lifecycle_identity("request:" + run),
        lifecycle_identity("invocation:" + run),
        lifecycle_identity("driver"),
        lifecycle_identity("policy"),
        lifecycle_identity("distribution"),
    )


class Port:
    def __init__(self, **options):
        self.options = options
        self.observed = []

    def execute(self, request):
        result = execution(request.run_identity.digest, **self.options)
        self.observed.append(result)
        return result


class QualificationCaseInventoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lifecycle_fixture.QualificationLifecycleRunnerTests()
        self.fixture.setUp()

    def test_distinct_components_can_share_case_content_without_losing_executions(self):
        port = Port()
        result = self.fixture.runner(lifecycle=port).run(self.fixture.plan)
        self.assertEqual([run.generated_test_total for run in result.runs], [9, 9])
        self.assertEqual(
            [len(run.generated_test_case_identities) for run in result.runs], [3, 3]
        )
        self.assertEqual(
            [len(run.generated_test_evidence_identities) for run in result.runs], [3, 3]
        )
        for actual in port.observed:
            nodes = actual.lifecycle.node_results
            self.assertEqual(len({node.component_revision for node in nodes}), 3)
            self.assertEqual(
                len(
                    {
                        node.generated_test_evidence.generated_test_suite_identity
                        for node in nodes
                    }
                ),
                3,
            )
            self.assertEqual(
                len(
                    {
                        case.observation_identity
                        for node in nodes
                        for case in node.generated_test_evidence.cases
                    }
                ),
                9,
            )
        self.assertEqual(
            QualificationLifecycleResult.from_dict(result.to_dict()), result
        )

    def test_already_unique_case_inventory_keeps_every_member_and_count(self):
        port = Port(shared=False)
        result = self.fixture.runner(lifecycle=port).run(self.fixture.plan)
        for run, actual in zip(result.runs, port.observed, strict=True):
            original = tuple(
                sorted(
                    (
                        case.case_identity
                        for node in actual.lifecycle.node_results
                        for case in node.generated_test_evidence.cases
                    ),
                    key=lambda row: row.uri,
                )
            )
            self.assertEqual(run.generated_test_case_identities, original)
            self.assertEqual(run.generated_test_total, 9)

    def test_inventory_cannot_hide_missing_or_duplicate_execution_or_wrong_total(
        self,
    ):
        for options in [{"missing": True}, {"duplicated": True}, {"receipt_total": 3}]:
            with (
                self.subTest(options=options),
                self.assertRaises(SourceToSpecificationError),
            ):
                self.fixture.runner(lifecycle=Port(**options)).run(self.fixture.plan)

    def test_failed_provider_or_duplicate_per_suite_case_is_still_refused(self):
        actual = execution("negative")
        suite = actual.lifecycle.node_results[0].generated_test_evidence
        with self.assertRaisesRegex(ValueError, "passed outcome"):
            replace(suite, outcome="failed")
        with self.assertRaisesRegex(ValueError, "identities|case IDs"):
            replace(
                suite,
                selected_case_identities=(
                    *suite.selected_case_identities,
                    suite.cases[0].case_identity,
                ),
                cases=(*suite.cases, suite.cases[0]),
                selected_count=4,
                executed_count=4,
                passed_count=4,
            )
