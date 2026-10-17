"""FINALIZE grants bind exact package intent and live private runtime authority."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import tests.support.fixtures_test_action_finalize_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
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

    def test_current_exact_grant_is_reread_on_every_check(self):
        self.guard(self.value)
        self.guard(self.value)
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(self.observer.call_count, 4)

    def test_revocation_after_initial_admission_is_observed(self):
        self.guard(self.value)
        self.grant = replace(self.grant, revoked=True)
        with self.assertRaises(ActionWireError) as error:
            self.guard(self.value)
        self.assertEqual(error.exception.code, "action_finalize.authority_invalid")

    def test_expired_and_not_yet_valid_grants_refuse(self):
        issued = self.now
        for now in (issued - timedelta(seconds=1), self.grant.expires_at):
            self.now = now
            with self.subTest(now=now), self.assertRaises(ActionWireError):
                self.guard(self.value)

    def test_other_package_does_not_reuse_existing_grant(self):
        with self.assertRaises(ActionWireError):
            self.guard(
                replace(self.value, package_result_identity=canonical_identity("other"))
            )
        self.provider.assert_not_called()

    def test_runtime_change_before_or_during_grant_lookup_refuses(self):
        for values in (
            [canonical_identity("other")],
            [self.runtime, canonical_identity("other")],
        ):
            self.observer.side_effect = values
            with self.subTest(values=values), self.assertRaises(ActionWireError):
                self.guard(self.value)

    def test_request_classification_privilege_profile_and_revision_must_match(self):
        original = self.grant
        for changes in (
            {"classification_digest": canonical_identity("other").uri},
            {"request_digest": canonical_identity("build-request").uri},
            {"effective_revision_digest": canonical_identity("other-revision").uri},
            {"privileges": ("execute-component",)},
            {"profile": SecurityProfile.BLOCKED},
        ):
            self.grant = replace(original, **changes)
            with (
                self.subTest(fields=tuple(changes)),
                self.assertRaises(ActionWireError),
            ):
                self.guard(self.value)

    def test_missing_grant_and_unknown_stage_refuse(self):
        self.grant = None
        with self.assertRaises(ActionWireError):
            self.guard(self.value)
        with self.assertRaises(ActionWireError):
            self.guard.require_stage("build", object())

    def test_scope_request_changes_with_runtime_or_package(self):
        baseline = self.guard.request
        self.assertNotEqual(
            baseline,
            finalize_execution_request(self.value, canonical_identity("other")),
        )
        self.assertNotEqual(
            baseline,
            finalize_execution_request(
                replace(
                    self.value, package_result_identity=canonical_identity("other")
                ),
                self.runtime,
            ),
        )
