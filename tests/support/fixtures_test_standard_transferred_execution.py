"""Shared fixtures extracted from ``tests.unit.test_standard_transferred_execution``."""

import unittest

from dataclasses import replace

from unittest.mock import patch

from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError

from literate_ai.adapters.qualification_capture import QualificationCaptureError

from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_inputs,
)

from literate_ai.contracts import canonical_identity, canonical_json_bytes

from literate_ai.security import AuthorizationError

from tests.support import fixtures_test_standard_transferred_tests as test_fixture

from tests.support.fixtures_test_component_node_generation_preparation import _fixture

class StandardTransferredExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = test_fixture.StandardTransferredTestTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.build = fixture.fixture
        self.controller = fixture.controller
        self.evidence = self.build.receiver.execute(
            self.build.plan, self.build.output.exports
        )
        self.refresh_records()

    def refresh_records(self):
        self.records = tuple(
            sorted(
                dict(
                    (
                        *self.fixture.records,
                        *self.build.receiver.retained_evidence_records(),
                    )
                ).items(),
                key=lambda item: item[0].uri,
            )
        )

    def scoped(self):
        _, execution_plan = _fixture()
        scope = plan_standard_execution_inputs(
            execution_plan, self.build.plan, self.build.output.exports, ()
        )
        self.evidence = self.build.receiver.execute_scoped(
            self.build.plan, self.build.output.exports, scope, ()
        )
        self.refresh_records()
        return scope

    def admit(self, **changes):
        arguments = dict(
            plan=self.build.plan,
            exports=self.build.output.exports,
            evidence=self.evidence,
            records=self.records,
            admission_guard=lambda: None,
        )
        arguments.update(changes)
        return self.controller.admit_transferred_execution(**arguments)

    def assert_unpublished(self):
        self.assertEqual(self.controller._execution_evidence, {})
        self.assertEqual(self.controller.execution_stdout, {})

    def test_actual_process_proof_and_stdout_without_controller_commands(self):
        with patch.object(
            self.controller,
            "_run_locked",
            side_effect=AssertionError("controller command"),
        ):
            self.assertEqual(self.admit(), self.evidence)
        self.assertEqual(
            self.controller._execution_evidence[self.evidence.identity.uri],
            self.evidence,
        )
        self.assertEqual(
            self.controller.execution_stdout, self.build.receiver.execution_stdout
        )
        self.assertTrue(
            set(self.records).issubset(set(self.controller.retained_evidence_records()))
        )

    def test_missing_process_or_stdout_refuses_before_publication(self):
        for identity in (
            self.evidence.observation_identity,
            self.evidence.stdout_identity,
        ):
            with self.subTest(identity=identity):
                with self.assertRaises(QualificationCaptureError):
                    self.admit(
                        records=tuple(
                            item for item in self.records if item[0] != identity
                        )
                    )
                self.assert_unpublished()

    def test_self_consistent_wrong_command_runtime_or_custody_refuses(self):
        for field in (
            "execution_contract_identity",
            "runtime_identity",
            "artifact_custody_identity",
        ):
            with self.subTest(field=field):
                evidence = replace(
                    self.evidence, **{field: canonical_identity("substituted")}
                )
                records = dict(self.records)
                records[evidence.identity] = canonical_json_bytes(evidence.to_dict())
                with self.assertRaises(ValueError):
                    self.admit(
                        evidence=evidence,
                        records=tuple(
                            sorted(records.items(), key=lambda item: item[0].uri)
                        ),
                    )
                self.assert_unpublished()

    def test_retention_and_guard_failure_leave_execution_unpublished(
        self,
    ):
        for mode in ("retention", "guard"):
            with self.subTest(mode=mode):
                state = {"retained": False}
                original = self.controller.retain_evidence_record

                def retain(
                    identity, content, mode=mode, original=original, state=state
                ):
                    if mode == "retention":
                        raise RuntimeError("retention failed")
                    original(identity, content)
                    state["retained"] = True

                def guard(state=state):
                    if state["retained"]:
                        raise RuntimeError("worker changed")

                with patch.object(
                    self.controller, "retain_evidence_record", side_effect=retain
                ):
                    with self.assertRaises(RuntimeError):
                        self.admit(admission_guard=guard)
                self.assert_unpublished()

    def test_unregistered_plan_refuses(self):
        self.controller._plans_by_revision.clear()
        with self.assertRaises(LocalStandardLifecycleError):
            self.admit()
        self.assert_unpublished()

    def test_scoped_process_requires_exact_current_scope(self):
        scope = self.scoped()
        wrong = replace(scope, execution_plan_identity=canonical_identity("other-plan"))
        for arguments in ({}, {"scope": wrong, "provider_artifacts": ()}):
            with self.assertRaises(ValueError):
                self.admit(**arguments)
            self.assert_unpublished()
        with patch.object(
            self.controller,
            "_run_locked",
            side_effect=AssertionError("controller command"),
        ):
            self.assertEqual(
                self.admit(scope=scope, provider_artifacts=()), self.evidence
            )
        self.assertEqual(
            self.controller.execution_stdout, self.build.receiver.execution_stdout
        )

    def test_scoped_grant_expiry_during_retention_refuses_publication(self):
        scope = self.scoped()
        original = self.controller.retain_evidence_record

        def retain(identity, content):
            original(identity, content)
            self.controller.clock = lambda: (
                self.evidence.execution_authority.grant.expires_at
            )

        with patch.object(
            self.controller, "retain_evidence_record", side_effect=retain
        ):
            with self.assertRaises(AuthorizationError):
                self.admit(scope=scope, provider_artifacts=())
        self.assert_unpublished()

