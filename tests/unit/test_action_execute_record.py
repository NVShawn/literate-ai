"""EXECUTE descriptors cannot substitute scope or bypass completed BUILD custody."""

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters import action_execute_record as records
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    canonical_identity,
    canonical_json_bytes,
)
from tests.unit import test_action_test_execution as fixture_module


class ExecuteWorkerRecordTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = fixture_module.ActionTestExecutionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        build = fixture.value.build_input
        output = fixture.value.build_result
        scope = plan_standard_execution_receipts(
            build.execution_plan, build.plan, output.evidence.exports, ()
        )
        self.value = records.ExecuteWorkerInput(build, output, scope)

    def admit(self, value=None):
        raw = (self.value if value is None else value).to_bytes()
        return records.ExecuteWorkerInput.admit(
            raw, record_identity(raw), self.fixture.deadline
        )

    def test_exact_build_and_recomputed_scope_roundtrip_without_host_commands(self):
        with patch.object(
            self.fixture.ports, "_run_locked", side_effect=AssertionError("command")
        ):
            self.assertEqual(self.admit(), self.value)
        self.assertEqual(self.value.provider_artifacts, ())
        contract = self.value.build_input.inputs.contract
        self.assertEqual(
            records.required_execute_toolchains(self.value.build_input.inputs),
            (contract.tool_binding(ComponentCommandPhase.EXECUTE).toolchain_identity,),
        )

    def test_foreign_scope_or_missing_runtime_closure_refuses(self):
        for field in ("execution_plan_identity", "build_plan_identity"):
            with self.subTest(field=field), self.assertRaises(ActionWireError):
                self.admit(
                    replace(
                        self.value,
                        scope=replace(
                            self.value.scope, **{field: canonical_identity("other")}
                        ),
                    )
                )
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(
                    self.value,
                    scope=replace(
                        self.value.scope,
                        transitive_provider_artifact_identities=(
                            canonical_identity("unaccepted-runtime"),
                        ),
                    ),
                )
            )

    def test_forged_completed_build_binding_refuses(self):
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(
                    self.value,
                    build_result=replace(
                        self.value.build_result,
                        input_identity=canonical_identity("foreign-input"),
                    ),
                )
            )

    def test_duplicate_noncanonical_unknown_and_oversized_records_refuse(self):
        raw = self.value.to_bytes()
        doc = json.loads(raw)
        changed = (
            raw[:-1] + b',"schema":"literate-ai/execute-worker-input@1"}',
            json.dumps(doc, indent=2).encode(),
            canonical_json_bytes({**doc, "extra": True}),
            canonical_json_bytes({**doc, "accepted_providers": [None] * 4097}),
        )
        for content in changed:
            with self.subTest(size=len(content)), self.assertRaises(ActionWireError):
                records.ExecuteWorkerInput.admit(
                    content, record_identity(content), self.fixture.deadline
                )
        with patch.object(records, "MAX_ACTION_RECORD_BYTES", len(raw) - 1):
            with self.assertRaises(ActionWireError):
                self.admit()

    def test_missing_or_unbound_provider_transfer_refuses(self):
        from tests.unit import test_action_provider_build as provider_module

        fixture = provider_module.ProviderBuildTransferTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        for transfers in ((), (fixture.transfer,)):
            with (
                self.subTest(transfers=len(transfers)),
                self.assertRaises(ActionWireError),
            ):
                self.admit(
                    replace(
                        self.value,
                        accepted_providers=(fixture.receipt,),
                        provider_builds=transfers,
                    )
                )
