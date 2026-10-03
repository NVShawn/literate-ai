"""ACCEPT result transfer must reopen complete proof without controller commands."""

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.action_accept_result import import_accept_result
from literate_ai.adapters.action_accept_result_record import AcceptWorkerResult
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_test_record import TestWorkerInput
from literate_ai.adapters.action_test_result import import_test_result
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.contracts import (
    StandardComponentAcceptanceEvidence,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.storage import FileSystemCAS
from tests.unit import test_action_accept_record as fixture_module


class AcceptWorkerResultTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.AcceptWorkerRecordTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.value = f.value
        self.worker = w = f.fixture
        self.ports = w.ports
        self.raw = self.value.to_bytes()
        self.identity = record_identity(self.raw)
        test_input = TestWorkerInput(
            w.value.build_input, w.value.build_result
        ).to_bytes()
        test_result = self.value.test_result.to_bytes()
        import_test_result(
            content=test_result,
            result_identity=record_identity(test_result),
            input_record=test_input,
            input_identity=record_identity(test_input),
            deadline=w.deadline,
            ports=self.ports,
            cas=w.cas,
            admission_guard=lambda: None,
        )
        policy = {
            "schema": "literate-ai/local-standard-acceptance-policy@1",
            "requires": ["build", "generated-tests", "execution"],
        }
        build = self.value.execution_input.build_input
        self.evidence = StandardComponentAcceptanceEvidence(
            component_revision=build.plan.component_revision,
            source_generation_identity=build.source_generation_identity,
            generated_test_suite_identity=build.candidate.generated_test_suite_identity,
            build=self.value.execution_input.build_result.evidence,
            generated_tests=self.value.test_result.evidence,
            execution=self.value.execution_result.evidence,
            acceptance_policy_identity=canonical_identity(policy),
        )
        refs = {}
        for result in (
            self.value.execution_input.build_result,
            self.value.test_result,
            self.value.execution_result,
        ):
            refs.update((ref.identity, ref) for ref in result.evidence_records)
        for doc in (policy, self.evidence.to_dict()):
            ref = w.cas.put_bytes(canonical_json_bytes(doc))
            refs[ref.identity] = ref
        self.result = AcceptWorkerResult(
            self.identity, self.evidence, tuple(refs[key] for key in sorted(refs))
        )
        self.cas = FileSystemCAS(w.fixture.root / "accept-return")

    def admit(self, result=None):
        raw = (result or self.result).to_bytes()
        return AcceptWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=self.raw,
            input_identity=self.identity,
            deadline=self.worker.deadline,
        )

    def transfer(self, result=None, **changes):
        raw = (result or self.result).to_bytes()
        args = dict(
            content=raw,
            result_identity=record_identity(raw),
            input_record=self.raw,
            input_identity=self.identity,
            deadline=self.worker.deadline,
            ports=self.ports,
            cas=self.cas,
            admission_guard=lambda: None,
            blob_source=self.worker.cas.get_bytes,
        )
        args.update(changes)
        return import_accept_result(**args)

    def test_roundtrip_and_actual_proof_transfer_without_commands(self):
        self.assertEqual(self.admit(), self.result)
        with (
            patch.object(
                self.ports, "_run_locked", side_effect=AssertionError("command")
            ),
            patch.object(
                self.ports, "accept", side_effect=AssertionError("local fallback")
            ),
        ):
            self.assertEqual(self.transfer(), self.evidence)
        retained = dict(self.ports.retained_evidence_records())
        self.assertEqual(
            retained[record_identity(self.result.to_bytes())], self.result.to_bytes()
        )

    def test_foreign_input_generation_and_policy_refuse(self):
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(self.result, input_identity=canonical_identity("foreign"))
            )
        for field in ("source_generation_identity", "acceptance_policy_identity"):
            evidence = replace(self.evidence, **{field: canonical_identity("foreign")})
            ref = self.worker.cas.put_bytes(canonical_json_bytes(evidence.to_dict()))
            refs = {item.identity: item for item in self.result.evidence_records}
            refs[ref.identity] = ref
            with self.subTest(field=field), self.assertRaises(ActionWireError):
                self.admit(
                    replace(
                        self.result,
                        evidence=evidence,
                        evidence_records=tuple(refs[key] for key in sorted(refs)),
                    )
                )

    def test_missing_deep_process_proof_refuses(self):
        missing = self.evidence.generated_tests.cases[0].observation_identity.uri
        result = replace(
            self.result,
            evidence_records=tuple(
                ref for ref in self.result.evidence_records if ref.identity != missing
            ),
        )
        self.admit(result)  # Descriptor validation cannot establish proof availability.
        with self.assertRaises(QualificationCaptureError):
            self.transfer(result)

    def test_changed_return_bytes_refuse(self):
        with self.assertRaises(ActionWireError):
            self.transfer(blob_source=lambda ref: b"corrupted")

    def test_worker_drift_during_fetch_refuses(self):
        state = {"changed": False}

        def fetch(ref):
            state["changed"] = True
            return self.worker.cas.get_bytes(ref)

        def guard():
            if state["changed"]:
                raise RuntimeError("worker changed")

        with self.assertRaisesRegex(RuntimeError, "worker changed"):
            self.transfer(blob_source=fetch, admission_guard=guard)

    def test_no_implicit_return_transport(self):
        from literate_ai.storage.cas import BlobNotFoundError

        with self.assertRaises(BlobNotFoundError):
            self.transfer(blob_source=None)

    def test_closed_result_and_duplicate_refs_refuse(self):
        raw = self.result.to_bytes()
        for content in (
            json.dumps(json.loads(raw), indent=2).encode(),
            raw[:-1] + b',"schema":"literate-ai/accept-worker-result@1"}',
            canonical_json_bytes({**json.loads(raw), "extra": True}),
        ):
            with self.assertRaises(ActionWireError):
                AcceptWorkerResult.admit(
                    content,
                    record_identity(content),
                    input_record=self.raw,
                    input_identity=self.identity,
                    deadline=self.worker.deadline,
                )
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(self.result, evidence_records=self.result.evidence_records * 2)
            )

    def test_result_proof_and_envelope_bounds(self):
        from literate_ai.adapters import action_accept_result_record as records

        for name in (
            "MAX_ACTION_RECORD_BYTES",
            "MAX_BUILD_EVIDENCE_BYTES",
            "MAX_BUILD_EVIDENCE_RECORDS",
        ):
            with (
                self.subTest(name=name),
                patch.object(records, name, 1),
                self.assertRaises(ActionWireError),
            ):
                self.admit()

    def test_failed_retention_cannot_return_receipt(self):
        with patch.object(
            self.ports,
            "retain_evidence_record",
            side_effect=RuntimeError("storage unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                self.transfer()
