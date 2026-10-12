"""Configured generation admits private authority and prompt before job allocation."""

import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.support.fixtures_test_action_generate_execution as fixture_module
from literate_ai.action_worker import main
from literate_ai.adapters.action_dispatch_wire import (
    ActionWireError,
    decode_action_response,
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.action_generate_result import GenerateWorkerResult
from literate_ai.adapters.action_generate_worker import ConfiguredGenerateWorker
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import ContentIdentity, canonical_identity
from tests.support.fixtures_test_action_generate_action import make_generate_request


class ConfiguredGenerateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.GenerateExecutionTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.request, self.records = make_generate_request(f.value, f.deadline)
        self.calls = []
        self.runtime = discover_python_toolchain(pinned_command=(sys.executable,))

    def worker(self, code="raise SystemExit(2)", guard=None):
        root = Path(__file__).resolve().parents[2]
        return ConfiguredGenerateWorker(
            LocalComponentToolBinding(
                sys.executable,
                ("-c", code),
                authority_identity=canonical_identity(
                    {"runtime": self.runtime.identity, "code": code}
                ),
                _authority_guard=self.runtime.require_unchanged,
            ),
            environment=dict(
                os.environ, PYTHONPATH=os.pathsep.join((str(root), str(root / "src")))
            ),
            authority_identity=canonical_identity("private-model-policy"),
            admission_guard=guard or self.calls.append,
        )

    def execute(self, worker=None, **changes):
        f = self.fixture
        return (worker or self.worker()).execute(
            **(
                dict(
                    request=self.request,
                    deadline=f.deadline,
                    records=self.records,
                    expected_worker_identity=self.request.worker.worker_identity,
                    cas=f.cas,
                    workspace_root=f.jobs,
                    blob_source=f.fixture.cas.get_bytes,
                )
                | changes
            )
        )

    def test_corrupt_prompt_and_cancel_refuse_before_allocating(self):
        for changes in ({"blob_source": lambda _: b"bad"}, {"cancelled": lambda: True}):
            with (
                self.subTest(changes=tuple(changes)),
                patch(
                    "literate_ai.adapters.action_generate_worker.tempfile.mkdtemp",
                    side_effect=AssertionError("allocated"),
                ),
                self.assertRaises(ActionWireError),
            ):
                self.execute(**changes)
        self.assertEqual(list(self.fixture.jobs.iterdir()), [])

    def test_private_authority_refusal_precedes_prompt_transport(self):
        def refuse(value):
            self.assertEqual(value, self.fixture.value)
            raise ActionWireError("fixture.denied", "model policy refused")

        with self.assertRaises(ActionWireError) as error:
            self.execute(
                self.worker(guard=refuse), blob_source=lambda _: self.fail("fetched")
            )
        self.assertEqual(error.exception.code, "fixture.denied")
        self.assertEqual(list(self.fixture.jobs.iterdir()), [])

    def test_child_failure_cleans_owned_directory(self):
        with self.assertRaises(ActionWireError) as error:
            self.execute()
        self.assertEqual(error.exception.code, "action_generate.process_failed")
        self.assertTrue(self.calls)
        self.assertTrue(all(value == self.fixture.value for value in self.calls))
        self.assertEqual(list(self.fixture.jobs.iterdir()), [])

    def test_actual_child_returns_verified_source_and_cleans_owned_directory(self):
        code = """
import os
from contextlib import contextmanager
from pathlib import Path
from literate_ai.generate_worker import main
from literate_ai.adapters.action_generate_execution import GenerationWorkerRuntime
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationRunner
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_cached_coding_cli_source_generation_runner import (
    CachedCodingCliSourceGenerationRunnerTests,
)
@contextmanager
def runtime(binding):
    f = CachedCodingCliSourceGenerationRunnerTests()
    f.setUp()
    try:
        runner = CachedCodingCliSourceGenerationRunner(
            f.generator, cas=FileSystemCAS(Path(os.environ['LITAI_GENERATE_CAS'])),
            invocation_provider=lambda _: f.invocation, workspace_binding=binding,
        )
        yield GenerationWorkerRuntime(f.node.definition, f.node.recipe, runner)
    finally: f.tearDown()
raise SystemExit(main(runtime_factory=runtime, admission_guard=lambda: None))
"""
        f = self.fixture
        # Source authority closure is privately provisioned; only prompt uses transport.
        for ref in f.fixture.cas.iter_refs():
            if ref != f.value.prompt:
                f.cas.put_bytes(f.fixture.cas.get_bytes(ref), media_type=ref.media_type)
        f.cas.put_bytes(f.fixture.cas.get_bytes(f.value.prompt))
        outcome, raw = self.receive(self.worker(code))
        self.assertIsNone(outcome.failure_code)
        result = GenerateWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=f.raw,
            input_identity=f.identity,
            deadline=f.deadline,
        )
        result.verify_records(
            tuple(
                (ContentIdentity.parse_uri(ref.identity), f.cas.get_bytes(ref))
                for ref in result.evidence_records
            )
        )
        self.assertEqual(list(f.jobs.iterdir()), [])

    def test_profile_binds_private_authority_and_authority_change_interrupts(self):
        first = self.worker()
        second = self.worker()
        second._authority_identity = canonical_identity("other-policy")
        self.assertNotEqual(first.identity, second.identity)
        marker = self.fixture.fixture.root / "started"
        code = (
            "import sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
            f"Path({str(marker)!r}).touch(); time.sleep(30)"
        )

        def guard(value):
            if marker.exists():
                raise ActionWireError("fixture.revoked", "authority revoked")

        with self.assertRaises(ActionWireError) as error:
            self.execute(self.worker(code, guard))
        self.assertEqual(error.exception.code, "fixture.revoked")
        self.assertEqual(list(self.fixture.jobs.iterdir()), [])

    def receive(self, worker):
        f = self.fixture
        output = io.BytesIO()
        wire = encode_action_request(self.request, f.deadline, self.records)
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
                ["--cas", str(f.cas.root), "--workspace", str(f.jobs)],
                generate_worker=worker,
            )
        self.assertEqual(status, 0)
        return decode_action_response(output.getvalue(), self.request)

    def test_unconfigured_receiver_refuses_generation(self):
        outcome, content = self.receive(None)
        self.assertEqual(outcome.failure_code, "action_generate.not_configured")
        self.assertIsNone(content)
