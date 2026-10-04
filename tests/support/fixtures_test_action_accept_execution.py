"""Shared fixtures extracted from ``tests.unit.test_action_accept_execution``."""

import shutil

import unittest

from contextlib import contextmanager

from dataclasses import replace

from unittest.mock import patch

from literate_ai.adapters.action_accept_execution import (
    execute_worker_accept,
    execute_worker_accept_from_cas,
)

from literate_ai.adapters.action_accept_result_record import AcceptWorkerResult

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity

from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts

from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceRecorder,
)

from literate_ai.storage import FileSystemCAS

from literate_ai.storage.cas import BlobNotFoundError

from tests.support import fixtures_test_action_accept_result as fixture_module

class AcceptWorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.AcceptWorkerResultTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.root = f.worker.fixture.root
        self.build = f.value.execution_input.build_input
        self.ports = self.new_ports(f.ports.source_trees, self.root / "accept-objects")
        self.ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=64 * 1024 * 1024, max_records=4096)
        )
        self.cas = FileSystemCAS(self.root / "accept-worker-cas")

    def new_ports(self, registry, root):
        return LocalStandardLifecyclePorts(
            source_trees=registry,
            object_root=root,
            contracts=tuple(self.fixture.ports.contracts.values()),
            tool_bindings=(),
            command_phases=(),
        )

    def fetch(self, ref):
        try:
            return self.fixture.worker.cas.get_bytes(ref)
        except BlobNotFoundError:
            return self.fixture.worker.source_cas.get_bytes(ref)

    def run_worker(self, **changes):
        f = self.fixture
        args = dict(
            input_record=f.raw,
            input_identity=f.identity,
            deadline=f.worker.deadline,
            ports=self.ports,
            cas=self.cas,
            blob_source=self.fetch,
        )
        args.update(changes)
        return execute_worker_accept(**args)

    def read_result(self, content):
        f = self.fixture
        return AcceptWorkerResult.admit(
            content,
            record_identity(content),
            input_record=f.raw,
            input_identity=f.identity,
            deadline=f.worker.deadline,
        )

    def test_actual_accept_on_tool_free_worker(self):
        with patch.object(
            self.ports, "_run_locked", side_effect=AssertionError("command")
        ):
            result = self.read_result(self.run_worker())
        self.assertEqual(result.evidence, self.fixture.evidence)
        self.assertEqual(self.ports.tool_bindings, {})
        for ref in result.evidence_records:
            self.cas.verify(ref)

    def test_missing_process_proof_refuses_before_accept(self):
        f = self.fixture
        result = f.value.test_result
        missing = result.evidence.cases[0].observation_identity.uri
        changed = replace(
            f.value,
            test_result=replace(
                result,
                evidence_records=tuple(
                    ref for ref in result.evidence_records if ref.identity != missing
                ),
            ),
        )
        raw = changed.to_bytes()
        with patch.object(
            self.ports,
            "accept",
            side_effect=AssertionError("accepted incomplete proof"),
        ):
            with self.assertRaises(QualificationCaptureError):
                self.run_worker(input_record=raw, input_identity=record_identity(raw))

    def test_corrupt_archive_refuses_before_accept(self):
        archive = self.fixture.value.execution_input.build_result.artifact_archive

        def fetch(ref):
            return b"corrupt" if ref.identity == archive.identity else self.fetch(ref)

        with patch.object(
            self.ports,
            "accept",
            side_effect=AssertionError("accepted corrupt artifact"),
        ):
            with self.assertRaises(ActionWireError):
                self.run_worker(blob_source=fetch)

    def test_artifact_changed_during_return_storage_refuses(self):
        original = self.cas.put_bytes

        def put(content, **kwargs):
            ref = original(content, **kwargs)
            if b"local-standard-acceptance-policy" in content:
                artifact = self.ports.artifact_path(
                    self.fixture.evidence.build.exports[0]
                )
                artifact.chmod(0o600)
                artifact.write_bytes(b"changed")
            return ref

        with patch.object(self.cas, "put_bytes", side_effect=put):
            with self.assertRaises((ValueError, RuntimeError)):
                self.run_worker()

    def test_cas_source_transfer_and_owned_cleanup(self):
        source = self.ports.source_trees.resolve(self.build.candidate.tree_identity)
        shutil.rmtree(source)
        workspace = self.root / "accept-job"
        workspace.mkdir()
        seen = []

        @contextmanager
        def factory(build, registry, recorder):
            seen.append(registry.resolve(build.candidate.tree_identity))
            ports = self.new_ports(registry, workspace / "objects")
            ports.retain_evidence_with(recorder)
            try:
                yield ports
            finally:
                shutil.rmtree(ports.object_root)

        f = self.fixture
        content = execute_worker_accept_from_cas(
            input_record=f.raw,
            input_identity=f.identity,
            deadline=f.worker.deadline,
            cas=self.cas,
            workspace_root=workspace,
            runtime_factory=factory,
            blob_source=self.fetch,
            owned_workspace=workspace,
        )
        self.assertEqual(self.read_result(content).evidence, f.evidence)
        self.assertFalse(seen[0].exists())
        self.assertEqual(list(workspace.iterdir()), [])

