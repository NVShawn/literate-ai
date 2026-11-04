"""Public retained SDK review must bind reproducible products, not a prior grant."""

import json
import os
import shutil
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

from literate_ai.adapters import retained_project_execution as execution
from literate_ai.adapters.native_sdk_acquisition import NativeSdkProjectAcquisition
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInput
from literate_ai.adapters.retained_project_inputs import verify_retained_project_inputs
from literate_ai.adapters.retained_project_runtime import execute_local_retained_project
from literate_ai.adapters.retained_project_sdk_runtime import RetainedProjectSdkRuntime
from literate_ai.contracts import canonical_identity
from literate_ai.storage.cas import FileSystemCAS
from tests.critical import test_retained_project_sdks as retained_fixtures
from tests.support import fixtures_test_native_sdk_source_build as producers


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc") and shutil.which("cmake"),
    "requires native POSIX CMake fixture",
)
class RetainedSdkPublicTests(unittest.TestCase):
    def setUp(self):
        self.fixture = producers.NativeSdkSourceBuildTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_fresh_producer_grants_preserve_selected_sdk_snapshot(self):
        first = self.fixture.service().build()
        second = self.fixture.service().build()
        self.assertEqual(first.selection.identity, second.selection.identity)
        self.assertEqual(
            first.product.snapshot.identity, second.product.snapshot.identity
        )
        self.assertNotEqual(
            NativeSdkConsumerInput(first).identity,
            NativeSdkConsumerInput(second).identity,
        )


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc") and shutil.which("cmake"),
    "requires native POSIX CMake fixture",
)
class PublicRetainedSdkRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        retained_fixtures.RetainedProjectSdkExecutionTests.setUpClass()
        cls.addClassCleanup(
            retained_fixtures.RetainedProjectSdkExecutionTests.doClassCleanups
        )

    def setUp(self):
        self.case = retained_fixtures.RetainedProjectSdkExecutionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture
        self.sdk_fixture = self.case.sdk_fixture
        self.binding = self.case.binding
        self.built = self.case.built
        self.lock = self.case.lock

    def public_runtime_arguments(self, **changes):
        return self.case.public_runtime_arguments(**changes)

    def test_public_local_runtime_builds_fresh_sdk_and_retains_real_command_evidence(
        self,
    ):
        args, kwargs = self.public_runtime_arguments()
        with patch(
            "literate_ai.adapters.retained_sdk_authority.project_component_roots",
            return_value=(self.sdk_fixture.recipe_fixture.component,),
        ):
            admission, workspace = execute_local_retained_project(*args, **kwargs)
            admission.require_current()
        self.assertEqual((workspace.outputs / "scale.txt").read_bytes(), b"4.5")
        owner = workspace._native_sdks.owners[0]
        (fresh,) = owner.for_scope(self.lock.root_revision)
        self.assertNotEqual(fresh.identity, self.binding.identity)
        self.assertEqual(
            fresh.build.product.snapshot.identity, self.built.product.snapshot.identity
        )
        self.assertEqual(admission.receipt.passed, 1)
        evidence = [
            admission.store.get_manifest(ref)
            for ref in admission.receipt.records
            if ref.media_type == "application/json"
        ]
        sdk_commands = [
            r
            for r in evidence
            if r.get("schema") == "literate-ai/native-sdk-command-execution@5"
        ]
        self.assertEqual(len(sdk_commands), 6)
        self.assertTrue(
            all(
                r["authorization"]["actor"] == "standard-retained-project"
                for r in sdk_commands
            )
        )
        self.assertFalse(admission.receipt.to_dict()["release_authorized"])

    def test_public_sdk_authorization_refuses_before_acquisition(self):
        args, kwargs = self.public_runtime_arguments()
        with patch(
            "literate_ai.adapters.retained_project_sdk_runtime.NativeSdkProjectAcquisition"
        ) as acquire:
            for changes in ({"authorization": None}, {"allow_host_execution": False}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    execute_local_retained_project(*args, **{**kwargs, **changes})
            acquire.assert_not_called()

    def test_foreign_recipe_refuses_before_sdk_build(self):
        args, kwargs = self.public_runtime_arguments(
            selection_identity=canonical_identity("foreign recipe")
        )
        with (
            patch(
                "literate_ai.adapters.retained_sdk_authority.project_component_roots",
                return_value=(self.sdk_fixture.recipe_fixture.component,),
            ),
            patch(
                "literate_ai.adapters.retained_project_sdk_runtime.NativeSdkProjectAcquisition"
            ) as acquire,
            self.assertRaisesRegex(ValueError, "complete-selected-recipes-required"),
        ):
            execute_local_retained_project(*args, **kwargs)
        acquire.assert_not_called()

    def test_changed_snapshot_refuses_before_consumer_execution(self):
        args, kwargs = self.public_runtime_arguments(
            snapshot_identity=canonical_identity("foreign snapshot")
        )
        with (
            patch(
                "literate_ai.adapters.retained_sdk_authority.project_component_roots",
                return_value=(self.sdk_fixture.recipe_fixture.component,),
            ),
            patch.object(execution, "run_bounded_process") as consumer,
            self.assertRaisesRegex(
                ValueError, "complete-selected-snapshot-closure-required"
            ),
        ):
            execute_local_retained_project(*args, **kwargs)
        consumer.assert_not_called()

    def test_revocation_after_real_sdk_command_prevents_admission(self):
        args, kwargs = self.public_runtime_arguments()
        grants = []
        original_authorize = RetainedProjectSdkRuntime.authorize
        original_execute = execution.run_bounded_process

        def authorize(session, *values):
            grant = original_authorize(session, *values)
            grants.append((session, grant))
            return grant

        def execute_then_revoke(*values, **options):
            result = original_execute(*values, **options)
            session, grant = grants[-1]
            session.revocations = session.revocations.revoke(
                grant.authorization_id,
                actor="fixture-operator",
                reason="Stop the executed SDK command",
            )
            return result

        with (
            patch(
                "literate_ai.adapters.retained_sdk_authority.project_component_roots",
                return_value=(self.sdk_fixture.recipe_fixture.component,),
            ),
            patch.object(RetainedProjectSdkRuntime, "authorize", authorize),
            patch.object(
                execution, "run_bounded_process", side_effect=execute_then_revoke
            ),
            self.assertRaisesRegex(ValueError, "revoked"),
        ):
            execute_local_retained_project(*args, **kwargs)
        self.assertTrue(grants)
        diagnostics = list(
            kwargs["cache_directories"].obj_dir.glob(
                "retained-project-runs/*/command-diagnostics.json"
            )
        )
        self.assertEqual(len(diagnostics), 1)
        document = json.loads(diagnostics[0].read_text())
        self.assertFalse(document["admission"])
        self.assertEqual(document["command_observations"], [])

    def test_authority_change_after_sdk_build_refuses_before_consumer_execution(self):
        args, kwargs = self.public_runtime_arguments()
        original = NativeSdkProjectAcquisition.__call__

        def acquire_then_change(acquisition, snapshot):
            owner = original(acquisition, snapshot)
            self.fixture.current = replace(
                self.fixture.plan,
                project_identity=canonical_identity("changed project"),
            )
            return owner

        with (
            patch(
                "literate_ai.adapters.retained_sdk_authority.project_component_roots",
                return_value=(self.sdk_fixture.recipe_fixture.component,),
            ),
            patch.object(NativeSdkProjectAcquisition, "__call__", acquire_then_change),
            patch.object(execution, "run_bounded_process") as consumer,
            self.assertRaisesRegex(ValueError, "project-authority-changed"),
        ):
            execute_local_retained_project(*args, **kwargs)
        consumer.assert_not_called()

    def _refuse_during_metadata_batch(self, change):
        args, kwargs = self.public_runtime_arguments()
        state = {}
        original_plan = kwargs["current_plan"]

        def current_plan():
            verify_retained_project_inputs(self.fixture.source, self.fixture.manifest)
            return original_plan()

        kwargs["current_plan"] = current_plan
        original_batch = RetainedProjectSdkRuntime.record_many
        original_authorize = RetainedProjectSdkRuntime.authorize
        original_put = FileSystemCAS.put_bytes

        def authorize(session, *values):
            grant = original_authorize(session, *values)
            state["grant"] = grant
            return grant

        def batch(session, documents, *, count):
            state["session"] = session
            state["active"] = True
            try:
                return original_batch(session, documents, count=count)
            finally:
                state["active"] = False

        def put_then_change(store, content, **options):
            reference = original_put(store, content, **options)
            if state.get("active") and "partial" not in state:
                state["partial"] = (store, reference)
                if change in {"source", "policy"}:
                    path = self.fixture.source / (
                        "src/main.txt" if change == "source" else "sdk-policy.json"
                    )
                    path.write_bytes(path.read_bytes() + b" ")
                elif change == "revocation":
                    session = state["session"]
                    session.revocations = session.revocations.revoke(
                        state["grant"].authorization_id,
                        actor="fixture-operator",
                        reason="Revoke during immutable metadata retention",
                    )
                else:
                    clock.now.return_value = state["grant"].expires_at
                    clock.now.side_effect = None
            return reference

        with (
            patch(
                "literate_ai.adapters.retained_sdk_authority.project_component_roots",
                return_value=(self.sdk_fixture.recipe_fixture.component,),
            ),
            patch.object(RetainedProjectSdkRuntime, "record_many", batch),
            patch.object(RetainedProjectSdkRuntime, "authorize", authorize),
            patch.object(FileSystemCAS, "put_bytes", put_then_change),
            patch.object(execution, "datetime") as clock,
        ):
            clock.now.side_effect = lambda *_: datetime.now(UTC)
            with self.assertRaises(ValueError):
                execute_local_retained_project(*args, **kwargs)
        # The real native consumer completed; a real immutable CAS blob may
        # remain, but no command observation, receipt or admission is published.
        store, reference = state["partial"]
        store.verify(reference)
        diagnostics = list(
            kwargs["cache_directories"].obj_dir.glob(
                "retained-project-runs/*/command-diagnostics.json"
            )
        )
        self.assertEqual(len(diagnostics), 1)
        document = json.loads(diagnostics[0].read_bytes())
        self.assertFalse(document["admission"])
        self.assertEqual(document["command_observations"], [])
        self.assertTrue(
            list(
                kwargs["cache_directories"].obj_dir.glob(
                    "retained-project-runs/*/outputs/scale.txt"
                )
            )
        )

    def test_source_change_during_metadata_batch_refuses_admission(self):
        self._refuse_during_metadata_batch("source")

    def test_sdk_policy_change_during_metadata_batch_refuses_admission(self):
        self._refuse_during_metadata_batch("policy")

    def test_revocation_during_metadata_batch_refuses_admission(self):
        self._refuse_during_metadata_batch("revocation")

    def test_expiry_during_metadata_batch_refuses_admission(self):
        self._refuse_during_metadata_batch("deadline")
