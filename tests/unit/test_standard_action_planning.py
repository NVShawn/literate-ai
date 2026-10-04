"""Production rebuild uses real PLAN dispatch and shared INDEX/PLAN capacity."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_plan_finalizer import CommandBuildPlanFinalizer
from literate_ai.contracts.identity import canonical_identity
from tests.support import fixtures_test_standard_action_indexing as factory_fixture


class StandardActionPlanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = factory_fixture.StandardActionIndexingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.admission.configure()
        self.pool = self.fixture.admission.pool()
        self.adapter = self.fixture.assemble(
            action_workers=self.pool,
            action_source_cas=self.fixture.admission.source.cas,
        )
        self.runtime = self.adapter.runtime
        self.indexer = self.runtime.application.lifecycle.indexer
        self.finalizer = self.runtime.application.lifecycle.build_plan_finalizer
        self.assertIsInstance(self.finalizer, CommandBuildPlanFinalizer)
        self.candidate = self.fixture.register(self.runtime)
        self.intent = self.runtime.lifecycle_ports.create(
            self.fixture.execution,
            self.fixture.execution.generation_plans[0],
            self.candidate,
            (),
            (),
        )
        index = self.indexer.index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.authorization = self.runtime.lifecycle_ports.authorize(self.intent, index)
        self.expected = self.runtime.lifecycle_ports.plan_finalization_inputs(
            self.intent, self.authorization
        ).finalize()

    def test_public_factory_dispatches_exact_plan_and_registers_local_custody(self):
        with patch.object(
            self.runtime.lifecycle_ports,
            "finalize",
            side_effect=AssertionError("unexpected local fallback"),
        ):
            result = self.finalizer.finalize(self.intent, self.authorization)
        self.assertEqual(result.to_dict(), self.expected.to_dict())
        self.assertIs(
            self.runtime.lifecycle_ports._plans_by_revision[
                self.intent.component_revision.uri
            ],
            result,
        )
        self.assertIsNotNone(self.runtime.checkpoint_store)
        self.assertIsNotNone(self.runtime.source_cache_restorer)

    def test_index_and_plan_reservations_cannot_oversubscribe_worker(self):
        index = self.indexer.try_reserve_index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.assertIsNotNone(index)
        self.assertIsNone(
            self.finalizer.try_reserve_plan(self.intent, self.authorization)
        )
        index.release()
        plan = self.finalizer.try_reserve_plan(self.intent, self.authorization)
        self.assertIsNotNone(plan)
        self.assertIsNone(
            self.indexer.try_reserve_index(
                self.candidate.component_revision, self.candidate.tree_identity
            )
        )
        self.assertEqual(plan.run().identity, self.expected.identity)
        recovered = self.indexer.try_reserve_index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.assertIsNotNone(recovered)
        recovered.release()

    def test_substituted_result_refuses_registration_and_releases_capacity(self):
        original = self.indexer._dispatcher

        def tampered(records, results):
            dispatcher = original(records, results)
            dispatch = dispatcher.dispatch

            def changed(request):
                outcome = dispatch(request)
                return replace(
                    outcome, result_identity=canonical_identity("foreign plan")
                )

            dispatcher.dispatch = changed
            return dispatcher

        with patch.object(self.indexer, "_dispatcher", side_effect=tampered):
            reservation = self.finalizer.try_reserve_plan(
                self.intent, self.authorization
            )
            with self.assertRaisesRegex(ActionWireError, "another build plan"):
                reservation.run()
        self.assertNotIn(
            self.intent.component_revision.uri,
            self.runtime.lifecycle_ports._plans_by_revision,
        )
        recovered = self.indexer.try_reserve_index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.assertIsNotNone(recovered)
        recovered.release()

    def test_result_retention_failure_cannot_register_plan(self):
        ports = self.runtime.lifecycle_ports
        plans_before = dict(ports._plans_by_revision)
        grants_before = dict(ports._sdk_build_authorizations)
        with patch.object(
            self.indexer,
            "remember_action_result",
            side_effect=RuntimeError("result store unavailable"),
        ):
            reservation = self.finalizer.try_reserve_plan(
                self.intent, self.authorization
            )
            self.assertIsNotNone(reservation)
            with self.assertRaisesRegex(RuntimeError, "result store unavailable"):
                reservation.run()
        self.assertEqual(ports._plans_by_revision, plans_before)
        self.assertEqual(ports._sdk_build_authorizations, grants_before)
        recovered = self.indexer.try_reserve_index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.assertIsNotNone(recovered)
        recovered.release()
