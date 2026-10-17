"""Generation results require exact source proof and current bounded input custody."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.action_generate_result import (
    GenerateWorkerResult,
    capture_generate_result,
)
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.contracts import (
    ComponentGenerationRuntimeObservation,
    ContentIdentity,
    canonical_identity,
)
from tests.support import (
    fixtures_test_cached_coding_cli_source_generation_runner as fixture_module,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class GenerateWorkerResultTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.CachedCodingCliSourceGenerationRunnerTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        _, execution = _fixture()
        self.value = GenerateWorkerInput.capture(
            execution, f.node, f.cas, self.deadline
        )
        self.input = self.value.to_bytes()
        self.identity = record_identity(self.input)
        self.output = f.runner(f.node)
        self.result = self.capture(self.output)

    def capture(self, output):
        return capture_generate_result(
            input_record=self.input,
            input_identity=self.identity,
            output=output,
            cas=self.fixture.cas,
            deadline=self.deadline,
        )

    def records(self, result=None):
        return tuple(
            (ContentIdentity.parse_uri(ref.identity), self.fixture.cas.get_bytes(ref))
            for ref in (result or self.result).evidence_records
        )

    def admit(self, result=None, input_record=None):
        raw = (result or self.result).to_bytes()
        source = self.input if input_record is None else input_record
        return GenerateWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=source,
            input_identity=record_identity(source),
            deadline=self.deadline,
        )

    def test_exact_result_roundtrip_reopens_complete_source_and_generation_proof(self):
        self.assertEqual(self.admit(), self.result)
        self.result.verify_records(self.records())
        self.assertEqual(self.result.output, self.output)

    def test_foreign_input_or_allocation_cannot_rebind_existing_output(self):
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(self.result, input_identity=canonical_identity("foreign"))
            )
        changed = replace(
            self.value, workspace_allocation_identity=canonical_identity("foreign")
        ).to_bytes()
        rebound = replace(self.result, input_identity=record_identity(changed))
        with self.assertRaises(ActionWireError):
            self.admit(rebound, input_record=changed)

    def test_missing_source_manifest_cannot_pass_proof_verification(self):
        changed = replace(
            self.result,
            evidence_records=tuple(
                ref
                for ref in self.result.evidence_records
                if ref.identity != self.output.candidate.source_manifest_identity.uri
            ),
        )
        admitted = self.admit(changed)
        with self.assertRaises(QualificationCaptureError):
            admitted.verify_records(self.records(changed))

    def test_corrupted_bytes_and_undeclared_records_refuse(self):
        records = self.records()
        damaged = ((records[0][0], b"corrupt"), *records[1:])
        with self.assertRaises(QualificationCaptureError):
            self.result.verify_records(damaged)
        extra = b"unrequested record"
        extended = tuple(
            sorted(
                (*records, (record_identity(extra), extra)),
                key=lambda item: item[0].uri,
            )
        )
        with self.assertRaises(ActionWireError):
            self.result.verify_records(extended)

    def test_duplicate_references_and_missing_output_record_refuse(self):
        for refs in (
            (self.result.evidence_records[0], *self.result.evidence_records),
            tuple(
                ref
                for ref in self.result.evidence_records
                if ref.identity != self.output.identity.uri
            ),
        ):
            with self.assertRaises(ActionWireError):
                self.admit(replace(self.result, evidence_records=refs))

    def test_over_budget_output_cannot_be_captured(self):
        changed = replace(
            self.output,
            runtime_observation=ComponentGenerationRuntimeObservation(
                self.value.request.budget.max_model_attempts + 1, 1, None, None
            ),
        )
        with self.assertRaises(ActionWireError):
            self.capture(changed)

    def test_noncanonical_or_wrong_hash_result_refuses(self):
        raw = self.result.to_bytes()
        for content, identity in (
            (json.dumps(json.loads(raw), indent=2).encode(), None),
            (raw, canonical_identity("foreign")),
        ):
            with self.assertRaises(ActionWireError):
                GenerateWorkerResult.admit(
                    content,
                    identity or record_identity(content),
                    input_record=self.input,
                    input_identity=self.identity,
                    deadline=self.deadline,
                )

    def test_expired_input_refuses_result_admission(self):
        expired = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        raw = self.result.to_bytes()
        with self.assertRaises(ActionWireError):
            GenerateWorkerResult.admit(
                raw,
                record_identity(raw),
                input_record=self.input,
                input_identity=self.identity,
                deadline=expired,
            )

    def test_generated_result_cannot_satisfy_retained_request(self):
        retained, _ = self.fixture._retained_input()
        value = GenerateWorkerInput.capture(
            self.value.execution_plan,
            self.fixture.node,
            self.fixture.cas,
            self.deadline,
            retained=retained,
            authorization=retained.identity.uri,
        )
        raw = value.to_bytes()
        rebound = replace(self.result, input_identity=record_identity(raw))
        with self.assertRaises(ActionWireError):
            self.admit(rebound, input_record=raw)
