"""Configured ACCEPT verifies proof before allocating jobs and owns child cleanup."""

import io
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.action_worker import main
from literate_ai.adapters.action_accept_result_record import AcceptWorkerResult
from literate_ai.adapters.action_accept_worker import ConfiguredAcceptWorker
from literate_ai.adapters.action_dispatch_wire import (
    ActionWireError,
    decode_action_response,
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.contracts import canonical_identity
from tests.unit import test_action_accept_execution as fixture_module
from tests.unit.test_action_accept_action import make_accept_request


class ConfiguredAcceptWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.AcceptWorkerExecutionTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.value = f.fixture.value
        self.deadline = f.fixture.worker.deadline
        self.request, self.records = make_accept_request(self.value, self.deadline)
        self.workspace = f.root / "configured-accept"
        self.workspace.mkdir()
        self.launcher = LocalComponentToolBinding(
            sys.executable, ("-c", "raise SystemExit(2)")
        )

    def worker(self, launcher=None):
        return ConfiguredAcceptWorker(
            launcher or self.launcher,
            environment=dict(
                os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")
            ),
        )

    def execute(self, worker=None, **changes):
        args = dict(
            request=self.request,
            deadline=self.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
            cas=self.fixture.cas,
            workspace_root=self.workspace,
            blob_source=self.fixture.fetch,
        )
        args.update(changes)
        return (worker or self.worker()).execute(**args)

    def test_actual_child_needs_no_application_tools_and_cleans_job(self):
        code = """
import os,shutil
from pathlib import Path
from contextlib import contextmanager
from literate_ai.accept_worker import main
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
@contextmanager
def factory(build,registry,recorder):
 ports=LocalStandardLifecyclePorts(source_trees=registry,object_root=Path(os.environ['LITAI_ACCEPT_WORKSPACE'])/'objects',contracts=(build.inputs.contract,),tool_bindings=(),command_phases=())
 ports.retain_evidence_with(recorder)
 try: yield ports
 finally: shutil.rmtree(ports.object_root)
raise SystemExit(main(runtime_factory=factory))
"""
        runtime = discover_python_toolchain(pinned_command=(sys.executable,))
        worker = self.worker(
            LocalComponentToolBinding(
                sys.executable,
                ("-c", code),
                authority_identity=canonical_identity(
                    {"runtime": runtime.identity, "code": code}
                ),
                _authority_guard=runtime.require_unchanged,
            )
        )
        self.assertEqual(worker.tools.identities, ())
        # Exercise the actual encoded receiver, with all proof supplied through CAS.
        for source in (
            self.fixture.fixture.worker.source_cas,
            self.fixture.fixture.worker.cas,
        ):
            for ref in source.iter_refs():
                self.fixture.cas.put_bytes(
                    source.get_bytes(ref), media_type=ref.media_type
                )
        outcome, content = self.receive(worker)
        self.assertIsNone(outcome.failure_code)
        raw = self.value.to_bytes()
        result = AcceptWorkerResult.admit(
            content,
            record_identity(content),
            input_record=raw,
            input_identity=record_identity(raw),
            deadline=self.deadline,
        )
        self.assertEqual(result.evidence, self.fixture.fixture.evidence)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_child_failure_cleans_owned_job(self):
        with self.assertRaises(ActionWireError):
            self.execute()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_missing_process_proof_refuses_before_job_allocation(self):
        stage = self.value.test_result
        missing = stage.evidence.cases[0].observation_identity.uri
        changed = replace(
            self.value,
            test_result=replace(
                stage,
                evidence_records=tuple(
                    ref for ref in stage.evidence_records if ref.identity != missing
                ),
            ),
        )
        request, records = make_accept_request(changed, self.deadline)
        with patch(
            "literate_ai.adapters.action_accept_worker.tempfile.mkdtemp",
            side_effect=AssertionError("job allocated"),
        ):
            with self.assertRaises(QualificationCaptureError):
                self.execute(request=request, records=records)

    def test_cancel_refuses_before_job_allocation(self):
        with patch(
            "literate_ai.adapters.action_accept_worker.tempfile.mkdtemp",
            side_effect=AssertionError("job allocated"),
        ):
            with self.assertRaises(ActionWireError):
                self.execute(cancelled=lambda: True)

    def test_corrupt_stage_bytes_refuse_before_job_allocation(self):
        identity = self.value.test_result.evidence.cases[0].observation_identity.uri

        def fetch(ref):
            return b"bad" if ref.identity == identity else self.fixture.fetch(ref)

        with patch(
            "literate_ai.adapters.action_accept_worker.tempfile.mkdtemp",
            side_effect=AssertionError("job allocated"),
        ):
            with self.assertRaises(ActionWireError):
                self.execute(blob_source=fetch)

    def receive(self, worker):
        output = io.BytesIO()
        wire = encode_action_request(self.request, self.deadline, self.records)
        with (
            patch(
                "literate_ai.action_worker.sys.stdin",
                SimpleNamespace(buffer=io.BytesIO(wire)),
            ),
            patch(
                "literate_ai.action_worker.sys.stdout", SimpleNamespace(buffer=output)
            ),
            patch.dict(
                os.environ,
                {
                    "LITAI_ACTION_WORKER_IDENTITY": (
                        self.request.worker.worker_identity.uri
                    )
                },
            ),
        ):
            status = main(
                [
                    "--cas",
                    str(self.fixture.cas.root),
                    "--workspace",
                    str(self.workspace),
                ],
                accept_worker=worker,
            )
        self.assertEqual(status, 0)
        return decode_action_response(output.getvalue(), self.request)

    def test_unconfigured_receiver_refuses_accept(self):
        outcome, content = self.receive(None)
        self.assertEqual(outcome.failure_code, "action_accept.not_configured")
        self.assertIsNone(content)
