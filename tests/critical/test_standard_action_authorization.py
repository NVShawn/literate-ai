"""Production AUTHORIZE uses real command transport and shared worker capacity."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_authorizer import CommandBuildAuthorizer
from literate_ai.contracts.identity import canonical_identity
from tests.support import fixtures_test_standard_action_planning as plan_fixture


class StandardActionAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = plan_fixture.StandardActionPlanningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        self.ports = self.runtime.lifecycle_ports
        self.indexer = self.fixture.indexer
        self.authorizer = self.runtime.application.lifecycle.authorizer
        self.assertIsInstance(self.authorizer, CommandBuildAuthorizer)
        self.intent = self.fixture.intent
        self.index = self.fixture.authorization.index_identity
        self.expected = self.fixture.authorization
        self.ports.clock = lambda: self.expected.grant.issued_at

    def test_substituted_result_never_reaches_local_admission(self):
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

        self._assert_refused_before_admission(
            patch.object(self.indexer, "_dispatcher", side_effect=tampered),
            ActionWireError,
            "another authorization",
        )

    def _assert_refused_before_admission(self, fault, error, message):
        with fault, patch.object(self.ports, "accept_build_authorization") as accept:
            reservation = self.authorizer.try_reserve_authorization(
                self.intent, self.index
            )
            with self.assertRaisesRegex(error, message):
                reservation.run()
            accept.assert_not_called()
        recovered = self.indexer.try_reserve_index(
            self.intent.component_revision, self.intent.source_tree_identity
        )
        self.assertIsNotNone(recovered)
        recovered.release()

    def test_changed_sdk_custody_refuses_returned_grant(self):
        with patch.object(
            self.ports,
            "_require_intent_sdk_inputs",
            side_effect=[None, RuntimeError("SDK changed")],
        ):
            with self.assertRaisesRegex(RuntimeError, "SDK changed"):
                self.authorizer.authorize(self.intent, self.index)
