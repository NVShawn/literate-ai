"""Shared fixtures extracted from ``tests.unit.test_command_builder``."""

import os

import shutil

import sys

import unittest

from types import SimpleNamespace

from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity

from literate_ai.adapters.command_builder import CommandComponentBuilder

from literate_ai.adapters.command_indexer import CommandGenerationIndexer

from literate_ai.adapters.lifecycle import LocalStandardLifecycleError

from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceRecorder,
)

from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionWorker,
)

from literate_ai.contracts import canonical_identity

from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)

from tests.support import fixtures_test_action_build_result as result_fixture

from tests.support.fixtures_test_standard_local_command_adapter import _provider_export

class CommandBuilderTests(unittest.TestCase):
    def setUp(self):
        fixture = result_fixture.ActionBuildResultTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.ports = fixture.controller
        self.plan = fixture.input.plan
        self.root = fixture.root
        self.marker = self.root / "command-ran"
        input_path = self.root / "expected-input.json"
        result_path = self.root / "result.json"
        input_path.write_bytes(fixture.input_record)
        result_path.write_bytes(fixture.content)
        # This worker returns an already-built, exact-input CAS result. The BUILD
        # operation's actual compilation is separately exercised by the child test.
        code = f"""
import sys
from pathlib import Path
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_WIRE_BYTES, decode_action_request, encode_action_response,
)
request, deadline, records = decode_action_request(
    sys.stdin.buffer.read(MAX_ACTION_WIRE_BYTES + 1)
)
assert request.action.kind.value == 'build'
expected = Path({str(input_path)!r}).read_bytes()
assert records[request.predecessor_result_identities[0]] == expected
Path({str(self.marker)!r}).write_text('executed')
sys.stdout.buffer.write(encode_action_response(
    request, result_record=Path({str(result_path)!r}).read_bytes()
))
"""
        self.worker = ExecutionWorker(
            "builder",
            ExecutionWorkerKind.COMMAND,
            command=(sys.executable, "-I", "-c", code),
        )
        self.catalog = ExecutionWorkerCatalog((self.worker,))
        self.admitted = LifecycleActionWorker(
            self.worker.worker_id,
            self.worker.identity,
            self.catalog.identity,
            canonical_identity("fixture-hardware"),
        )
        self.healthy = True

        def revalidate(worker):
            self.assertEqual(worker, self.admitted)
            if not self.healthy:
                raise ActionWireError(
                    "fixture.worker_changed", "worker admission changed"
                )

        self.indexer = CommandGenerationIndexer(
            fixture.input.execution_plan,
            self.ports.source_trees,
            lambda source: self.ports.source_trees.evidence(source).candidate,
            fixture.cas,
            self.catalog,
            (self.admitted,),
            fixture.deadline,
            cwd=self.root,
            revalidate_worker=revalidate,
            environment=dict(os.environ),
        )
        self.admission = SimpleNamespace(
            catalog=self.catalog,
            workers=(self.admitted,),
            identity=canonical_identity("fixture-admission"),
            supports_build=lambda worker, tools: worker == self.admitted,
            supports_phase=lambda worker, phase: (
                worker == self.admitted and phase is LifecycleActionKind.BUILD
            ),
        )
        self.recorder = QualificationEvidenceRecorder(
            max_bytes=64 * 1024 * 1024, max_records=4096
        )
        self.ports.retain_evidence_with(self.recorder)
        self.builder = CommandComponentBuilder(
            self.indexer, self.admission, self.ports, result_source=self.fetch
        )

    def fetch(self, worker, reference):
        self.assertEqual(worker, self.worker)
        return self.fixture.worker_cas.get_bytes(reference)

    def assert_unregistered(self):
        self.assertEqual(self.ports._artifact_paths, {})
        self.assertEqual(list(self.ports.object_root.iterdir()), [])
        reservation = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reservation)
        reservation.release()

    def test_command_result_is_verified_retained_and_runs_generated_test(self):
        shutil.rmtree(self.fixture.worker.object_root)
        with patch.object(
            self.ports, "build", side_effect=AssertionError("local fallback")
        ):
            output = self.builder.build(self.plan, ())
        self.assertTrue(self.marker.exists())
        handoff = self.builder.test_handoff(self.plan, output.exports)
        self.assertEqual(handoff.build_input, self.fixture.input)
        self.assertEqual(handoff.build_result.evidence, output.evidence)
        self.assertEqual(output, self.fixture.fixture.output)
        retained = dict(self.ports.retained_evidence_records())
        self.assertEqual(
            retained[self.fixture.input_identity], self.fixture.input_record
        )
        self.assertEqual(
            retained[record_identity(self.fixture.content)], self.fixture.content
        )
        self.assertEqual(
            self.ports.test(self.plan, output.exports).component_revision,
            self.plan.component_revision,
        )

    def test_test_handoff_requires_successful_build_and_exact_exports(self):
        with self.assertRaises(ActionWireError):
            self.builder.test_handoff(self.plan, self.fixture.fixture.output.exports)
        output = self.builder.build(self.plan, ())
        with self.assertRaises(ActionWireError):
            self.builder.test_handoff(self.plan, ())
        self.assertEqual(
            self.builder.test_handoff(self.plan, output.exports).build_input.plan,
            self.plan,
        )

    def test_build_reserves_the_indexer_capacity_and_releases_after_result(self):
        held = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNone(self.builder.try_reserve_build(self.plan, ()))
        held.release()
        reserved = self.builder.try_reserve_build(self.plan, ())
        self.assertIsNone(self.indexer.slots.try_reserve(lambda worker, slot: None))
        self.assertEqual(reserved.run(), self.fixture.fixture.output)
        next_slot = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(next_slot)
        next_slot.release()

    def test_provider_or_recorder_refusal_precedes_command_execution(self):
        with self.assertRaises(ActionWireError):
            self.builder.build(self.plan, (_provider_export("foreign"),))
        self.ports._evidence_recorder = None
        with self.assertRaises(LocalStandardLifecycleError):
            self.builder.build(self.plan, ())
        self.assertFalse(self.marker.exists())
        self.assert_unregistered()

    def test_missing_build_capability_refuses_controller_composition(self):
        self.admission.supports_phase = lambda worker, phase: False
        with self.assertRaisesRegex(ValueError, "no admitted BUILD"):
            CommandComponentBuilder(self.indexer, self.admission, self.ports)
        self.assertFalse(self.marker.exists())

    def test_no_exact_tool_match_refuses_before_reservation_or_dispatch(self):
        self.admission.supports_build = lambda worker, tools: False
        with patch.object(self.indexer.slots, "try_reserve") as reserve:
            with self.assertRaises(ActionWireError) as raised:
                self.builder.try_reserve_build(self.plan, ())
            self.assertEqual(raised.exception.code, "action_build.tools_unavailable")
            reserve.assert_not_called()
        self.assertFalse(self.marker.exists())
        self.assert_unregistered()

    def test_foreign_provider_receipt_refuses_before_dispatch(self):
        from tests.support.fixtures_test_action_build_intent import provider_evidence

        self.builder.retain_build_provider_evidence(self.plan, ())
        with self.assertRaises(ActionWireError):
            self.builder.retain_build_provider_evidence(
                self.plan, (provider_evidence(canonical_identity("foreign")),)
            )
        self.assertFalse(self.marker.exists())
        self.assert_unregistered()

    def test_retention_limit_refuses_before_dispatch(self):
        self.ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=1, max_records=1)
        )
        with self.assertRaises(QualificationCaptureError):
            self.builder.build(self.plan, ())
        self.assertFalse(self.marker.exists())
        self.assert_unregistered()

    def test_worker_change_during_fetch_prevents_registration_and_cleans_stage(self):
        def fetch(worker, reference):
            self.healthy = False
            return self.fetch(worker, reference)

        self.builder.result_source = fetch
        with self.assertRaises(ActionWireError):
            self.builder.build(self.plan, ())
        self.assertTrue(self.marker.exists())
        self.assert_unregistered()

    def test_corrupt_worker_blob_cannot_fall_back_or_register_artifacts(self):
        self.builder.result_source = lambda worker, reference: b"corrupt"
        with (
            patch.object(
                self.ports, "build", side_effect=AssertionError("local fallback")
            ),
            self.assertRaises(ActionWireError),
        ):
            self.builder.build(self.plan, ())
        self.assertTrue(self.marker.exists())
        self.assert_unregistered()

if __name__ == "__main__":
    unittest.main()

