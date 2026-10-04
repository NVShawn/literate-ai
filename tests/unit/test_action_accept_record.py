"""ACCEPT binds actual completed stages, with no caller-selected acceptance policy."""

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters import action_accept_record as records
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.action_test_execution import execute_worker_test
from literate_ai.adapters.action_test_record import TestWorkerInput, TestWorkerResult
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.contracts import (
    ComponentCommandPhase,
    canonical_identity,
    canonical_json_bytes,
)
from tests.support import fixtures_test_action_execute_execution as fixture_module


class AcceptWorkerRecordTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.ActionExecuteExecutionTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        raw = f.execute()
        executed = ExecuteWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=f.content,
            input_identity=f.identity,
            deadline=f.deadline,
        )
        ports = LocalStandardLifecyclePorts(
            source_trees=f.ports.source_trees,
            object_root=f.fixture.root / "accept-test",
            contracts=tuple(f.ports.contracts.values()),
            tool_bindings=tuple(f.ports.tool_bindings.values()),
            command_phases=(ComponentCommandPhase.TEST,),
        )
        ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=64 * 1024 * 1024, max_records=4096)
        )
        test_raw = TestWorkerInput(f.value.build_input, f.value.build_result).to_bytes()
        result_raw = execute_worker_test(
            input_record=test_raw,
            input_identity=record_identity(test_raw),
            deadline=f.deadline,
            ports=ports,
            cas=f.cas,
            blob_source=f.source_cas.get_bytes,
        )
        tests = TestWorkerResult.admit(
            result_raw,
            record_identity(result_raw),
            input_record=test_raw,
            input_identity=record_identity(test_raw),
            deadline=f.deadline,
        )
        self.value = records.AcceptWorkerInput(f.value, tests, executed)

    def admit(self, value=None):
        raw = (self.value if value is None else value).to_bytes()
        return records.AcceptWorkerInput.admit(
            raw, record_identity(raw), self.fixture.deadline
        )

    def test_actual_completed_stages_roundtrip_without_commands(self):
        with patch.object(
            self.fixture.ports, "_run_locked", side_effect=AssertionError("command")
        ):
            self.assertEqual(self.admit(), self.value)

    def test_foreign_stage_input_refuses(self):
        for field in ("test_result", "execution_result"):
            with self.subTest(field=field), self.assertRaises(ActionWireError):
                self.admit(
                    replace(
                        self.value,
                        **{
                            field: replace(
                                getattr(self.value, field),
                                input_identity=canonical_identity("foreign"),
                            )
                        },
                    )
                )

    def test_wrong_build_in_either_stage_refuses(self):
        for field in ("test_result", "execution_result"):
            result = getattr(self.value, field)
            evidence = replace(
                result.evidence,
                build_evidence_identity=canonical_identity("foreign-build"),
            )
            with self.subTest(field=field), self.assertRaises(ActionWireError):
                self.admit(
                    replace(self.value, **{field: replace(result, evidence=evidence)})
                )

    def test_closed_canonical_envelope_and_no_policy_override(self):
        raw = self.value.to_bytes()
        value = json.loads(raw)
        for content in (
            raw[:-1] + b',"schema":"literate-ai/accept-worker-input@1"}',
            json.dumps(value, indent=2).encode(),
            canonical_json_bytes({**value, "policy": "weaker"}),
            canonical_json_bytes({**value, "test_result": None}),
            b"[]",
            b"null",
            b"\xff",
        ):
            with self.subTest(size=len(content)), self.assertRaises(ActionWireError):
                records.AcceptWorkerInput.admit(
                    content, record_identity(content), self.fixture.deadline
                )
        with self.assertRaises(ActionWireError):
            records.AcceptWorkerInput.admit(
                raw, canonical_identity("wrong"), self.fixture.deadline
            )

    def test_combined_proof_bounds_and_envelope_bound(self):
        for name in (
            "MAX_BUILD_EVIDENCE_RECORDS",
            "MAX_BUILD_EVIDENCE_BYTES",
            "MAX_ACTION_RECORD_BYTES",
        ):
            with (
                self.subTest(name=name),
                patch.object(records, name, 1),
                self.assertRaises(ActionWireError),
            ):
                self.admit()

    def test_contradictory_shared_blob_descriptor_refuses(self):
        shared = self.value.execution_input.build_result.evidence_records[0]
        result = self.value.test_result
        refs = {ref.identity: ref for ref in result.evidence_records}
        refs[shared.identity] = replace(shared, size=shared.size + 1)
        changed = replace(
            result, evidence_records=tuple(refs[key] for key in sorted(refs))
        )
        with self.assertRaises(ActionWireError):
            self.admit(replace(self.value, test_result=changed))
