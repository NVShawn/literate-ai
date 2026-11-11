"""Real Standard record/stage reopening with explicit authored admission fixtures.

Generation, host/native execution, current plan admission and independent oracle
comparison are simulated. Hashes, complete Standard stage/receipt/source/suite/BOM
checks and byte-read ordering are real. This is not genuine qualification or
receiving admission; isolated current-oracle controls live in comparison tests.
"""

import unittest
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.authored_qualification_reopening import (
    reopen_authored_qualification,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    reopen_qualification_lifecycle,
)
from literate_ai.contracts.authored_qualification_acceptance import (
    AuthoredNodeIndependentAcceptance,
)
from literate_ai.contracts.authored_qualification_comparisons import (
    AuthoredQualificationComparison,
)
from literate_ai.contracts.authored_qualification_products import (
    AuthoredQualificationProducts,
    AuthoredQualificationRunProducts,
)
from literate_ai.contracts.authored_qualification_requests import (
    AuthoredQualificationLifecycleRequest,
)
from literate_ai.contracts.authored_qualification_runs import (
    AuthoredQualificationNodeRun,
    AuthoredQualificationResult,
    AuthoredQualificationRun,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.testing import ProjectTestReceipt
from tests.critical import test_authored_qualification_runs as authored
from tests.support import fixtures_test_qualification_capture as legacy


class AuthoredQualificationReopeningTests(unittest.TestCase):
    def setUp(self):
        class Producer(legacy.QualificationCaptureTests):
            def library_authority_fixture(this, **kwargs):
                value = super().library_authority_fixture(**kwargs)
                this.producer = value[0]
                return value

        fixture = Producer("runTest")
        self.fixture = fixture
        result, captures, entries = fixture.typed_product_fixture(
            bind_current_target=True
        )
        self.recorder = QualificationEvidenceRecorder(
            max_bytes=8_000_000, max_records=3000
        )
        for _identity, payload in entries:
            self.recorder.remember_bytes(payload)
        f = authored.AuthoredQualificationRunTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.plan = replace(
            f.plan,
            execution_plan=fixture.producer.execution,
            driver_identity=result.runs[0].driver_identity,
            lifecycle_policy_identity=result.runs[0].lifecycle_policy_identity,
            framework_distribution_identity=result.runs[
                0
            ].framework_distribution_identity,
        )
        lock = fixture.current_lock
        self.assertEqual(self.plan.origin.component_lock_identity, lock.identity)
        self.recorder.remember_json(lock.to_dict())
        for n in lock.nodes:
            self.recorder.remember_json(n.revision.definition.to_dict())
        runs, members = [], []
        for ordinal, (old, capture) in enumerate(
            zip(result.runs, captures, strict=True)
        ):
            run_id = self.recorder.remember_json(
                {
                    "schema": "literate-ai/authored-qualification-run-selection@1",
                    "plan_identity": self.plan.identity.uri,
                    "session_identity": canonical_identity("fixture-session").uri,
                    "ordinal": ordinal,
                }
            )
            allocation = canonical_identity(
                {
                    "schema": "literate-ai/qualification-workspace-allocation@1",
                    "session_identity": canonical_identity("fixture-session").uri,
                    "run_identity": run_id.uri,
                }
            )
            lifecycle = reopen_qualification_lifecycle(
                self.reader(), old.lifecycle_result_identity
            )
            receipt = ProjectTestReceipt.from_dict(
                self.reader().read_json(old.project_receipt_identity)
            )
            nodes = tuple(
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
            comparison = AuthoredQualificationComparison(
                self.plan.origin,
                self.plan.identity,
                run_id,
                self.plan.oracle_set_identity,
                lifecycle.root_integration.independent_acceptance_identity,
                tuple(
                    AuthoredNodeIndependentAcceptance(
                        n.component_revision,
                        canonical_identity("fixture-oracle"),
                        canonical_identity("fixture-cases"),
                        canonical_identity(
                            ["fixture-observation", ordinal, n.component_revision.uri]
                        ),
                        1,
                    )
                    for n in nodes
                ),
            )
            run = AuthoredQualificationRun(
                AuthoredQualificationLifecycleRequest(
                    self.plan.origin,
                    self.plan.identity,
                    self.plan.oracle_set_identity,
                    run_id,
                    allocation,
                ),
                old.driver_identity,
                old.lifecycle_policy_identity,
                old.framework_distribution_identity,
                old.lifecycle_request_identity,
                old.lifecycle_invocation_identity,
                old.lifecycle_result_identity,
                receipt,
                lifecycle.root_integration,
                comparison,
                nodes,
            )
            runs.append(run)
            self.recorder.remember_json(run.to_dict())
            members.append(
                AuthoredQualificationRunProducts(
                    run_id, run.identity, capture.exports.identity
                )
            )
        self.result = AuthoredQualificationResult(self.plan, tuple(runs))
        self.recorder.remember_json(self.result.to_dict())
        self.products = AuthoredQualificationProducts(
            self.plan.origin,
            self.result.identity,
            tuple(sorted(members, key=lambda m: m.run_identity.uri)),
        )
        self.recorder.remember_json(self.products.to_dict())
        bindings = tuple(
            SimpleNamespace(
                component_revision_identity=r,
                oracle=SimpleNamespace(
                    import_surface_identity=c.library_import_surface.identity
                ),
            )
            for r, c in fixture.current_commands.items()
        )
        self.current = CurrentAuthoredQualificationPlan(
            self.plan,
            SimpleNamespace(
                authority=SimpleNamespace(
                    locked=SimpleNamespace(authority=SimpleNamespace(lock=lock))
                ),
                bindings=bindings,
                capture_current=nullcontext,
            ),
            None,
            None,
            None,
            None,
        )
        self.blob_ids = {
            ContentIdentity.parse_uri(p.artifact_export.blob.identity)
            for c in captures
            for p in c.exports.libraries
        }
        self.product_reads = []
        self.active = True
        self.expire_on_product_read = False
        self.fail_second_comparison = False
        self.substitute_comparison = False

    def reader(self, entries=None):
        return QualificationEvidenceReader(
            self.recorder.entries if entries is None else entries,
            max_bytes=8_000_000,
            max_records=3000,
        )

    def reopen(
        self,
        *,
        entries=None,
        current=None,
        recipes=None,
        commands=None,
        max_package_bytes=4096,
    ):
        reader = self.reader(entries)
        read = reader.read_bytes

        def observe(identity):
            if identity in self.blob_ids:
                self.product_reads.append(identity)
                if self.expire_on_product_read:
                    self.active = False
            return read(identity)

        reader.read_bytes = observe
        comparisons = {r.request.run_identity: r.comparison for r in self.result.runs}

        def compare(_current, *, run_identity, **kwargs):
            if (
                self.fail_second_comparison
                and run_identity == self.result.runs[1].request.run_identity
            ):
                raise QualificationCaptureError("fixture independent comparison failed")
            value = comparisons[run_identity]
            if self.substitute_comparison:
                value = replace(
                    value,
                    nodes=(
                        replace(
                            value.nodes[0],
                            observation_identity=canonical_identity(
                                "foreign-observation"
                            ),
                        ),
                        *value.nodes[1:],
                    ),
                )
            return value

        def guard():
            if not self.active:
                raise ValueError("fixture current owner expired")

        with (
            patch.object(
                CurrentAuthoredQualificationPlan, "require_unchanged", side_effect=guard
            ),
            patch(
                "literate_ai.adapters.authored_qualification_runs.compare_authored_qualification",
                side_effect=compare,
            ) as comparator,
            patch(
                "subprocess.run", side_effect=AssertionError("executed archive content")
            ),
        ):
            result = reopen_authored_qualification(
                self.current if current is None else current,
                reader,
                self.products.identity,
                current_recipes=self.fixture.current_recipes
                if recipes is None
                else recipes,
                current_commands=self.fixture.current_commands
                if commands is None
                else commands,
                max_package_bytes=max_package_bytes,
            )
            self.assertEqual(comparator.call_count, 2)
            return result

    def test_both_complete_runs_reopen_through_real_shared_stage_checks(self):
        reopened = self.reopen()
        self.assertEqual(reopened.membership.result, self.result)
        self.assertEqual(len(reopened.executions), 2)
        self.assertEqual(len(self.product_reads), 8)
        self.assertEqual(
            {i for i, _e in reopened.executions},
            {r.request.run_identity for r in self.result.runs},
        )

    def test_decoded_plan_cannot_restore_current_owner(self):
        with self.assertRaises(TypeError):
            self.reopen(current=self.plan)
        self.assertEqual(self.product_reads, [])

    def test_missing_or_extra_current_projection_refuses_before_product_read(self):
        for name in ("recipes", "commands"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    QualificationCaptureError, "authority_incomplete"
                ):
                    self.reopen(**{name: {}})
        self.assertEqual(self.product_reads, [])

    def test_current_plan_drift_refuses_before_product_read(self):
        current = replace(
            self.current,
            plan=replace(self.plan, output_root="litai/qualification/other"),
        )
        with self.assertRaisesRegex(QualificationCaptureError, "reopen_plan_mismatch"):
            self.reopen(current=current)
        self.assertEqual(self.product_reads, [])

    def test_missing_second_run_stage_refuses_before_any_product_read(self):
        node = self.result.runs[1].nodes[0]
        missing = node.acceptance.build.build_observation_identity
        self.assertIsInstance(missing, ContentIdentity)
        entries = tuple((i, b) for i, b in self.recorder.entries if i != missing)
        with self.assertRaisesRegex(QualificationCaptureError, "record-missing"):
            self.reopen(entries=entries)
        self.assertEqual(self.product_reads, [])

    def test_foreign_current_recipe_refuses_before_product_read(self):
        recipes = dict(self.fixture.current_recipes)
        key = next(iter(recipes))
        recipes[key] = replace(recipes[key], recipe_id="foreign-current-recipe")
        with self.assertRaises(QualificationCaptureError):
            self.reopen(recipes=recipes)
        self.assertEqual(self.product_reads, [])

    def test_current_command_substitution_refuses_before_product_read(self):
        commands = dict(self.fixture.current_commands)
        key = next(iter(commands))
        commands[key] = replace(
            commands[key],
            locked_build_authority_identity=canonical_identity("foreign-build"),
        )
        with self.assertRaises(QualificationCaptureError):
            self.reopen(commands=commands)
        self.assertEqual(self.product_reads, [])

    def test_second_run_comparison_failure_refuses_before_any_product_read(self):
        self.fail_second_comparison = True
        with self.assertRaisesRegex(QualificationCaptureError, "comparison failed"):
            self.reopen()
        self.assertEqual(self.product_reads, [])

    def test_recomputed_comparison_must_match_the_exact_retained_run(self):
        self.substitute_comparison = True
        with self.assertRaisesRegex(QualificationCaptureError, "reopen_run_mismatch"):
            self.reopen()
        self.assertEqual(self.product_reads, [])

    def test_current_owner_expiry_during_product_reads_returns_no_reopening(self):
        self.expire_on_product_read = True
        with self.assertRaisesRegex(ValueError, "owner expired"):
            self.reopen()
        self.assertTrue(self.product_reads)

    def test_unique_product_budget_is_checked_before_product_reads(self):
        entries = dict(self.recorder.entries)
        unique_bytes = sum(len(entries[i]) for i in self.blob_ids)
        with self.assertRaisesRegex(QualificationCaptureError, "byte-limit"):
            self.reopen(max_package_bytes=unique_bytes - 1)
        self.assertEqual(self.product_reads, [])
        reopened = self.reopen(max_package_bytes=unique_bytes)
        self.assertEqual(len(reopened.executions), 2)
