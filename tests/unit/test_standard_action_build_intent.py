"""Production BUILD_INTENT dispatch, shared slots and independent result custody."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_build_intent import CommandBuildIntentDispatcher
from literate_ai.contracts.identity import canonical_identity
from tests.unit import test_standard_action_indexing as factory_fixture


class StandardActionBuildIntentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = factory_fixture.StandardActionIndexingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.admission.configure()
        pool = self.fixture.admission.pool()
        self.runtime = self.fixture.assemble(
            action_workers=pool, action_source_cas=self.fixture.admission.source.cas
        ).runtime
        service = self.runtime.application.lifecycle
        self.indexer = service.indexer
        self.dispatcher = service.build_intent_dispatcher
        self.assertIsInstance(self.dispatcher, CommandBuildIntentDispatcher)
        self.candidate = self.fixture.register(self.runtime)
        self.index = self.indexer.index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.arguments = (
            self.fixture.execution,
            self.fixture.execution.generation_plans[0],
            self.candidate,
            self.index,
            (),
            (),
            (),
        )
        self.expected = self.runtime.lifecycle_ports.build_intent_inputs(
            *self.arguments[:3], (), ()
        ).create()

    def assert_unregistered(self):
        self.assertNotIn(
            self.expected.identity.uri, self.runtime.lifecycle_ports._intent_artifacts
        )
        self.assertNotIn(
            self.expected.identity.uri,
            self.runtime.lifecycle_ports._intent_package_artifacts,
        )
        self.assertNotIn(
            self.expected.identity.uri,
            self.runtime.lifecycle_ports._library_consumer_bindings,
        )

    def test_factory_dispatches_exact_intent_before_local_registration(self):
        self.assert_unregistered()
        with patch.object(
            self.runtime.lifecycle_ports,
            "create",
            side_effect=AssertionError("local fallback"),
        ):
            reservation = self.dispatcher.try_reserve_intent(*self.arguments)
            result = reservation.run()
        self.assertEqual(result, self.expected)
        self.assertIn(
            result.identity.uri, self.runtime.lifecycle_ports._intent_artifacts
        )
        self.assertIsNotNone(self.runtime.checkpoint_store)
        self.assertIsNotNone(self.runtime.source_cache_restorer)

    def test_index_intent_and_plan_share_one_worker_slot(self):
        index = self.indexer.try_reserve_index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.assertIsNone(self.dispatcher.try_reserve_intent(*self.arguments))
        index.release()
        intent = self.dispatcher.try_reserve_intent(*self.arguments)
        self.assertIsNone(
            self.indexer.try_reserve_index(
                self.candidate.component_revision, self.candidate.tree_identity
            )
        )
        authorization = self.runtime.lifecycle_ports.authorize(
            self.expected, self.index
        )
        self.assertIsNone(
            self.runtime.application.lifecycle.build_plan_finalizer.try_reserve_plan(
                self.expected, authorization
            )
        )
        self.assertEqual(intent.run(), self.expected)
        restored = self.indexer.try_reserve_index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.assertIsNotNone(restored)
        restored.release()

    def test_substituted_result_releases_without_registration(self):
        original = self.indexer._dispatcher

        def tampered(records, results):
            dispatcher = original(records, results)
            dispatch = dispatcher.dispatch

            def changed(request):
                return replace(
                    dispatch(request), result_identity=canonical_identity("foreign")
                )

            dispatcher.dispatch = changed
            return dispatcher

        self._assert_refused_without_registration(
            patch.object(self.indexer, "_dispatcher", side_effect=tampered),
            ActionWireError,
            "another build intent",
        )

    def test_recording_failure_releases_without_registration(self):
        self._assert_refused_without_registration(
            patch.object(
                self.indexer,
                "remember_action_result",
                side_effect=RuntimeError("recording refused"),
            ),
            RuntimeError,
            "recording refused",
        )

    def _assert_refused_without_registration(self, fault, error, message):
        with fault, self.assertRaisesRegex(error, message):
            self.dispatcher.try_reserve_intent(*self.arguments).run()
        self.assert_unregistered()
        restored = self.indexer.try_reserve_index(
            self.candidate.component_revision, self.candidate.tree_identity
        )
        self.assertIsNotNone(restored)
        restored.release()

    def test_wrong_index_refuses_before_dispatch(self):
        arguments = (
            *self.arguments[:3],
            canonical_identity("foreign index"),
            *self.arguments[4:],
        )
        with (
            patch.object(self.indexer, "_dispatcher") as dispatch,
            self.assertRaisesRegex(ActionWireError, "current index differs"),
        ):
            self.dispatcher.try_reserve_intent(*arguments).run()
        dispatch.assert_not_called()
        self.assert_unregistered()
