"""Shared test fixtures extracted from test_action_test_result."""

import json
import shutil
import unittest
from unittest.mock import Mock, patch

from literate_ai.adapters.action_build_result import import_build_result
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_test_result import import_test_result
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.blobs import BlobRef
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import BlobIntegrityError, BlobNotFoundError
from tests.support import fixtures_test_action_test_execution as worker_fixture


class ActionTestResultTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = worker_fixture.ActionTestExecutionTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.content = f.execute()
        self.result_identity = record_identity(self.content)
        build = f.value.build_input
        root = f.fixture.root
        self.ports = LocalStandardLifecyclePorts(
            source_trees=f.ports.source_trees,
            object_root=root / "result-controller",
            contracts=tuple(f.ports.contracts.values()),
            tool_bindings=(),
            command_phases=(),
        )
        self.ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=64 * 1024 * 1024, max_records=4096)
        )
        self.ports.accept_build_intent(
            build.execution_plan,
            build.generation_plan,
            build.candidate,
            build.inputs.providers,
            build.inputs.package_artifacts,
            build.inputs.intent,
        )
        self.ports.accept_finalized_plan(
            build.inputs.intent, build.inputs.authorization, build.plan
        )
        self.cas = FileSystemCAS(root / "result-controller-cas")
        raw_input = build.to_bytes()
        raw_result = f.value.build_result.to_bytes()
        import_build_result(
            content=raw_result,
            result_identity=record_identity(raw_result),
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=f.deadline,
            ports=self.ports,
            cas=self.cas,
            retain_record=self.ports.retain_evidence_record,
            blob_source=f.cas.get_bytes,
        )
        shutil.rmtree(f.ports.object_root)

    def import_result(self, **changes):
        f = self.fixture
        arguments = dict(
            content=self.content,
            result_identity=self.result_identity,
            input_record=f.content,
            input_identity=f.identity,
            deadline=f.deadline,
            ports=self.ports,
            cas=self.cas,
            admission_guard=lambda: None,
            blob_source=f.cas.get_bytes,
        )
        arguments.update(changes)
        return import_test_result(**arguments)

    def test_real_result_is_retained_and_admitted_without_controller_test_commands(
        self,
    ):
        fetch = Mock(side_effect=self.fixture.cas.get_bytes)
        with patch.object(
            self.ports, "test", side_effect=AssertionError("controller TEST")
        ):
            result = self.import_result(blob_source=fetch)
            self.assertGreater(fetch.call_count, 0)
            fetch.reset_mock()
            self.assertEqual(self.import_result(blob_source=fetch), result)
            fetch.assert_not_called()
        self.assertEqual(result.passed_count, 3)
        self.assertEqual(self.ports._test_evidence[result.identity.uri], result)
        self.assertEqual(
            dict(self.ports.retained_evidence_records())[self.result_identity],
            self.content,
        )

    def test_corrupt_or_unavailable_remote_bytes_leave_test_unregistered(self):
        def missing(reference):
            raise FileNotFoundError("worker evidence unavailable")

        for fetch in (lambda ref: b"corrupt", missing):
            with self.subTest(fetch=fetch):
                with self.assertRaises((ActionWireError, FileNotFoundError)):
                    self.import_result(blob_source=fetch)
                self.assertEqual(self.ports._test_evidence, {})

    def test_missing_return_transport_does_not_infer_a_source(self):
        with self.assertRaises(BlobNotFoundError):
            self.import_result(blob_source=None)
        self.assertEqual(self.ports._test_evidence, {})

    def test_corrupt_cached_evidence_refuses_without_refetch(self):
        references = [
            BlobRef.from_dict(item)
            for item in json.loads(self.content)["evidence_records"]
        ]
        reference = references[0]
        self.cas.put_bytes(
            self.fixture.cas.get_bytes(reference), media_type=reference.media_type
        )
        path = self.cas.path_for(reference)
        path.chmod(0o600)
        path.write_bytes(b"changed cached evidence")
        fetch = Mock(side_effect=AssertionError("refetch corrupted cache"))
        with self.assertRaises(BlobIntegrityError):
            self.import_result(blob_source=fetch)
        fetch.assert_not_called()
        self.assertEqual(self.ports._test_evidence, {})

    def test_plan_change_during_fetch_prevents_registration(self):
        def fetch(reference):
            payload = self.fixture.cas.get_bytes(reference)
            self.ports._plans_by_revision.clear()
            return payload

        with self.assertRaises(LocalStandardLifecycleError):
            self.import_result(blob_source=fetch)
        self.assertEqual(self.ports._test_evidence, {})

    def test_substituted_response_identity_refuses_before_fetch(self):
        value = json.loads(self.content)
        value["input_identity"] = canonical_identity("other").uri
        changed = canonical_json_bytes(value)
        fetch = Mock(side_effect=AssertionError("fetch"))
        with self.assertRaises(ActionWireError):
            self.import_result(
                content=changed,
                result_identity=record_identity(changed),
                blob_source=fetch,
            )
        fetch.assert_not_called()
        self.assertEqual(self.ports._test_evidence, {})

    def test_changed_worker_after_transfer_refuses_before_retention(self):
        state = {"changed": False}

        def fetch(reference):
            result = self.fixture.cas.get_bytes(reference)
            state["changed"] = True
            return result

        def guard():
            if state["changed"]:
                raise RuntimeError("worker changed")

        with self.assertRaisesRegex(RuntimeError, "worker changed"):
            self.import_result(blob_source=fetch, admission_guard=guard)
        self.assertEqual(self.ports._test_evidence, {})
        self.assertNotIn(
            self.result_identity, dict(self.ports.retained_evidence_records())
        )

    def test_response_retention_failure_leaves_test_unregistered(self):
        original = self.ports.retain_evidence_record

        def retain(identity, content):
            if identity == self.result_identity:
                raise RuntimeError("response retention failed")
            original(identity, content)

        with patch.object(self.ports, "retain_evidence_record", side_effect=retain):
            with self.assertRaisesRegex(RuntimeError, "response retention failed"):
                self.import_result()
        self.assertEqual(self.ports._test_evidence, {})

    def test_changed_worker_after_response_retention_prevents_registration(self):
        state = {"changed": False}
        original = self.ports.retain_evidence_record

        def retain(identity, content):
            original(identity, content)
            if identity == self.result_identity:
                state["changed"] = True

        def guard():
            if state["changed"]:
                raise RuntimeError("worker changed")

        with patch.object(self.ports, "retain_evidence_record", side_effect=retain):
            with self.assertRaisesRegex(RuntimeError, "worker changed"):
                self.import_result(admission_guard=guard)
        self.assertEqual(self.ports._test_evidence, {})
