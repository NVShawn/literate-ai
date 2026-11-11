"""Admission around complete Standard composition, with explicit fixture owners.

These controls compose the real Standard service and typed run records. Current
plan review and oracle comparison are injected; native commands and generation
remain simulated. Existing planning/comparison tests own their real filesystem
and isolated-oracle coverage. No genuine model or installed qualification here.
"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.authored_qualification_runs import (
    observe_authored_qualification_run,
)
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleExecution,
)
from tests.critical import test_authored_qualification_planning as planning
from tests.critical import test_authored_qualification_runs as records
from tests.support import fixtures_test_qualification_lifecycle_runner as legacy
from tests.support import fixtures_test_standard_project_lifecycle as standard


class AuthoredQualificationRunObservationTests(unittest.TestCase):
    def setUp(self):
        f = records.AuthoredQualificationRunTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        p = planning.AuthoredQualificationPlanningTests("runTest")
        p.setUp()
        self.addCleanup(p.doCleanups)
        self.original = p.plan()
        # Deliberately injected current-owner check for the simulated diamond;
        # do not imply these different fixture graphs share genuine authority.
        self.current = replace(self.original, plan=f.plan)
        self.run = f.runs[0]
        self.execution = f.executions["one"]

    def observe(self, execution=None, request=None):
        return observe_authored_qualification_run(
            self.current,
            request=request or self.run.request,
            execution=execution or self.execution,
            reader=None,  # Comparison is the explicit injected fixture boundary.
        )

    def test_observer_retains_exact_standard_nodes_and_original_request(self):
        with (
            patch.object(
                CurrentAuthoredQualificationPlan, "require_unchanged"
            ) as guard,
            patch(
                "literate_ai.adapters.authored_qualification_runs.compare_authored_qualification",
                return_value=self.run.comparison,
            ) as compare,
        ):
            result = self.observe()
            self.assertEqual(result, self.run)
            self.assertGreaterEqual(guard.call_count, 2)
            compare.assert_called_once()
            self.assertEqual(result.generated_test_total, 4)

    def test_exact_native_binding_and_request_refuse_before_comparison(self):
        changed = records.identity("substituted")
        with (
            patch.object(CurrentAuthoredQualificationPlan, "require_unchanged"),
            patch(
                "literate_ai.adapters.authored_qualification_runs.compare_authored_qualification"
            ) as compare,
        ):
            for field in (
                "driver_identity",
                "lifecycle_policy_identity",
                "framework_distribution_identity",
            ):
                with self.subTest(field=field):
                    with self.assertRaisesRegex(ValueError, "run_plan_mismatch"):
                        self.observe(
                            execution=replace(self.execution, **{field: changed})
                        )
            with self.assertRaisesRegex(ValueError, "run_plan_mismatch"):
                self.observe(
                    request=replace(self.run.request, oracle_set_identity=changed)
                )
            compare.assert_not_called()

    def test_successful_root_only_standard_run_cannot_replace_complete_graph(self):
        f = self.fixture.fixture
        lock = standard._single_component_lock(f.lock, "money")
        execution_plan, requests = standard._prepared_execution(lock)
        ports = standard.ContractEvidenceLifecyclePorts(
            execution_plan, standard._names(lock)
        )
        result = standard._service(ports).execute(
            execution_plan,
            component_lock=lock,
            invalidation=standard._decision(
                execution_plan, standard._names(lock), "money", ("money",)
            ),
            prepared_nodes=standard._prepared_nodes(execution_plan, requests),
        )
        self.assertTrue(result.successful)
        execution = replace(
            self.execution, lifecycle=result, receipt=legacy._receipt(result)
        )
        with (
            patch.object(CurrentAuthoredQualificationPlan, "require_unchanged"),
            patch(
                "literate_ai.adapters.authored_qualification_runs.compare_authored_qualification"
            ) as compare,
        ):
            with self.assertRaisesRegex(ValueError, "run_membership_mismatch"):
                self.observe(execution=execution)
            compare.assert_not_called()

    def test_shared_standard_cache_and_receipt_controls_cannot_be_bypassed(self):
        cached = legacy._lifecycle("cached", hit=True)
        execution = QualificationLifecycleExecution(
            cached,
            legacy._receipt(cached),
            self.execution.lifecycle_request_identity,
            self.execution.lifecycle_invocation_identity,
            self.execution.driver_identity,
            self.execution.lifecycle_policy_identity,
            self.execution.framework_distribution_identity,
        )
        with (
            patch.object(CurrentAuthoredQualificationPlan, "require_unchanged"),
            patch(
                "literate_ai.adapters.authored_qualification_runs.compare_authored_qualification"
            ) as compare,
        ):
            with self.assertRaisesRegex(
                ValueError, "source-cache hits|source-cache|Source-cache"
            ):
                self.observe(execution=execution)
            compare.assert_not_called()

    def test_decoded_plan_never_restores_current_owner(self):
        with self.assertRaises(TypeError):
            observe_authored_qualification_run(
                self.fixture.plan,
                request=self.run.request,
                execution=self.execution,
                reader=None,
            )
        # The actual unpatched original current owner still detects drift.
        path = self.original.oracles.authority.project.root / "litai/qualification.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            observe_authored_qualification_run(
                self.original,
                request=self.run.request,
                execution=self.execution,
                reader=None,
            )
