"""GENERATION supervision binds private authority, controls and child proof."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.support.fixtures_test_action_generate_execution as fixture_module
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_generate_process import run_generate_worker_process
from literate_ai.adapters.action_generate_result import GenerateWorkerResult
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import ContentIdentity, canonical_identity


class GenerateProcessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.GenerateExecutionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        for ref in f.fixture.cas.iter_refs():
            f.cas.put_bytes(f.fixture.cas.get_bytes(ref), media_type=ref.media_type)

    def run_child(self, code, **changes):
        f = self.fixture
        arguments = dict(
            launcher=LocalComponentToolBinding(sys.executable, ("-c", code)),
            input_record=f.raw,
            input_identity=f.identity,
            deadline=f.deadline,
            cwd=f.fixture.root,
            environment=dict(
                os.environ,
                PYTHONPATH=os.pathsep.join(
                    (
                        str(Path(__file__).resolve().parents[2]),
                        str(Path(__file__).resolve().parents[2] / "src"),
                    )
                ),
            ),
            admission_guard=f.guard,
            cas_root=f.cas.root,
            workspace_root=f.jobs,
        )
        arguments.update(changes)
        return run_generate_worker_process(**arguments)

    def test_exact_input_and_case_insensitive_private_controls(self):
        code = (
            "import sys,os,json; print(json.dumps({'input':"
            "sys.stdin.buffer.read().decode(),'controls':{k:v for k,v in "
            "os.environ.items() if k.lower().startswith('litai_generate_')}}))"
        )
        f = self.fixture
        result = json.loads(
            self.run_child(
                code,
                environment=dict(
                    os.environ,
                    litai_generate_cas="foreign",
                    LITAI_GENERATE_INPUT_IDENTITY="foreign",
                ),
            )
        )
        self.assertEqual(result["input"], f.raw.decode())
        self.assertEqual(
            result["controls"],
            {
                "LITAI_GENERATE_INPUT_IDENTITY": f.identity.uri,
                "LITAI_GENERATE_DEADLINE": f.deadline.expires_at.isoformat(),
                "LITAI_GENERATE_CAS": str(f.cas.root),
                "LITAI_GENERATE_WORKSPACE": str(f.jobs),
            },
        )

    def test_invalid_input_or_cancelled_action_never_launches(self):
        with patch(
            "literate_ai.adapters.action_worker_process.run_bounded_process",
            side_effect=AssertionError("launched"),
        ):
            for changes in (
                {"input_record": b"bad", "input_identity": record_identity(b"bad")},
                {"cancelled": lambda: True},
                {"cas_root": Path("relative")},
            ):
                with (
                    self.subTest(changes=tuple(changes)),
                    self.assertRaises(ActionWireError),
                ):
                    self.run_child("pass", **changes)

    def test_private_authority_loss_during_child_interrupts_it(self):
        marker = self.fixture.fixture.root / "started"
        code = (
            "import sys,time; from pathlib import Path; sys.stdin.buffer.read(); "
            f"Path({str(marker)!r}).touch(); time.sleep(30)"
        )

        def guard():
            if marker.exists():
                raise ActionWireError(
                    "fixture.authority_changed", "private authority changed"
                )

        with self.assertRaises(ActionWireError) as error:
            self.run_child(code, admission_guard=guard)
        self.assertEqual(error.exception.code, "fixture.authority_changed")

    def test_oversized_output_and_failed_child_refuse(self):
        for code, expected in (
            (
                "import sys; sys.stdin.buffer.read(); "
                f"sys.stdout.buffer.write(b'x'*{MAX_ACTION_RECORD_BYTES + 1})",
                "action_generate.output_oversized",
            ),
            (
                "import sys; sys.stdin.buffer.read(); raise SystemExit(2)",
                "action_generate.process_failed",
            ),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaises(ActionWireError) as error,
            ):
                self.run_child(code)
            self.assertEqual(error.exception.code, expected)

    def test_actual_entrypoint_runs_production_generator_and_returns_verified_proof(
        self,
    ):
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
        runtime = discover_python_toolchain(pinned_command=(sys.executable,))
        f = self.fixture
        raw = self.run_child(
            code,
            launcher=LocalComponentToolBinding(
                sys.executable,
                ("-c", code),
                authority_identity=canonical_identity(
                    {"runtime": runtime.identity, "code": code}
                ),
                _authority_guard=runtime.require_unchanged,
            ),
        )
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
        self.assertEqual(
            result.output.candidate.workspace_allocation_identity,
            f.value.workspace_allocation_identity,
        )
        self.assertEqual(list(f.jobs.iterdir()), [])
