"""FINALIZE grants bind exact package intent and live private runtime authority."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import tests.support.fixtures_test_action_finalize_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_finalize_authority import (
    FinalizeExecutionGuard,
    finalize_execution_request,
)
from literate_ai.contracts import canonical_identity
from literate_ai.security import BuildAuthorization, SecurityProfile


class FinalizeAuthorityTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.FinalizeWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.value = f.value
        self.runtime = canonical_identity("private-runtime-and-oracle")
        request = finalize_execution_request(self.value, self.runtime)
        self.now = datetime.now(UTC)
        self.grant = BuildAuthorization(
            "fixture-finalize",
            record_identity(self.value.to_bytes()).uri,
            canonical_identity(request.to_dict()).uri,
            self.value.component_lock.root_revision.uri,
            "fixture",
            "explicit packaged-project execution",
            SecurityProfile.CONSTRAINED,
            request.requested_privileges,
            self.now,
            self.now + timedelta(minutes=5),
        )
        self.provider = Mock(side_effect=lambda: self.grant)
        self.observer = Mock(return_value=self.runtime)
        self.guard = FinalizeExecutionGuard(
            self.value,
            self.runtime,
            grant_provider=self.provider,
            observe_runtime=self.observer,
            clock=lambda: self.now,
        )
