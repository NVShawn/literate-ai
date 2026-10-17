"""Encoded LINK requests execute through the command receiver with real proof."""

import os
import subprocess
import sys
import unittest

import tests.support.fixtures_test_action_link as fixture_module
from literate_ai.adapters.action_dispatch_wire import (
    decode_action_response,
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.action_link_result import LinkWorkerResult
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server


class LinkReceiverTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.LinkActionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.jobs = f.source.root.parent / "link-receiver-jobs"
        self.jobs.mkdir()

    def run_receiver(self, *, cas, url=None, identity=None):
        f = self.fixture
        command = [
            sys.executable,
            "-I",
            "-m",
            "literate_ai.action_worker",
            "--cas",
            str(cas.root),
            "--workspace",
            str(self.jobs),
        ]
        if url is not None:
            command.extend(("--source-cas-url", url, "--allow-http"))
        return subprocess.run(
            command,
            input=encode_action_request(f.request, f.fixture.deadline, f.records),
            capture_output=True,
            timeout=30,
            env=dict(
                os.environ,
                LITAI_ACTION_WORKER_IDENTITY=identity
                or f.request.worker.worker_identity.uri,
            ),
        )

    def test_actual_command_transfers_proof_and_returns_verified_link(self):
        f = self.fixture
        refs = (
            *f.value.acceptance_result.evidence_records,
            f.value.acceptance_input.execution_input.build_result.artifact_archive,
        )
        blobs = {blob_path(ref): f.source.get_bytes(ref) for ref in refs}
        with source_cas_server(blobs) as (url, reads):
            process = self.run_receiver(cas=f.cas, url=url)
        self.assertEqual(process.returncode, 0, process.stderr.decode())
        outcome, raw = decode_action_response(process.stdout, f.request)
        self.assertIsNone(outcome.failure_code)
        result = LinkWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=f.raw,
            input_identity=record_identity(f.raw),
            deadline=f.fixture.deadline,
        )
        self.assertEqual(result.manifest, f.value.manifest)
        self.assertTrue(reads)
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_missing_explicit_transport_returns_failure(self):
        f = self.fixture
        process = self.run_receiver(cas=f.cas)
        outcome, raw = decode_action_response(process.stdout, f.request)
        self.assertIsNotNone(outcome.failure_code)
        self.assertIsNone(raw)
        self.assertEqual(list(self.jobs.iterdir()), [])
