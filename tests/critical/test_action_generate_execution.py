"""Worker GENERATE runs admitted source production and cleans only owned staging."""

import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_generate_execution import (
    GenerationWorkerRuntime,
    execute_worker_generation,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.action_generate_result import GenerateWorkerResult
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationRunner
from literate_ai.contracts import ContentIdentity
from literate_ai.storage import FileSystemCAS
from tests.support import (
    fixtures_test_cached_coding_cli_source_generation_runner as fixture_module,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class GenerateExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.CachedCodingCliSourceGenerationRunnerTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        _, execution = _fixture()
        self.value = GenerateWorkerInput.capture(
            execution, f.node, f.cas, self.deadline
        )
        self.raw = self.value.to_bytes()
        self.identity = record_identity(self.raw)
        self.cas = FileSystemCAS(f.root / "worker-cas")
        self.jobs = f.root / "jobs"
        self.jobs.mkdir()
        self.opened = []
        self.healthy = True

    def guard(self):
        if not self.healthy:
            raise RuntimeError("private authority changed")

    @contextmanager
    def runtime(self, binding):
        f = self.fixture
        self.opened.append(binding)
        runner = CachedCodingCliSourceGenerationRunner(
            f.generator,
            cas=self.cas,
            invocation_provider=lambda _: f.invocation,
            workspace_binding=binding,
        )
        yield GenerationWorkerRuntime(f.node.definition, f.node.recipe, runner)

    def execute(self, **changes):
        arguments = dict(
            input_record=self.raw,
            input_identity=self.identity,
            deadline=self.deadline,
            cas=self.cas,
            workspace_root=self.jobs,
            runtime_factory=self.runtime,
            admission_guard=self.guard,
            blob_source=self.fixture.cas.get_bytes,
        )
        arguments.update(changes)
        return execute_worker_generation(**arguments)

    def test_actual_runner_returns_complete_proof_from_separate_cas(self):
        raw = self.execute()
        result = GenerateWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=self.raw,
            input_identity=self.identity,
            deadline=self.deadline,
        )
        result.verify_records(
            tuple(
                (ContentIdentity.parse_uri(ref.identity), self.cas.get_bytes(ref))
                for ref in result.evidence_records
            )
        )
        self.assertEqual(
            result.output.candidate.workspace_allocation_identity,
            self.value.workspace_allocation_identity,
        )
        self.assertEqual(len(self.fixture.generator.calls), 1)
        self.assertEqual(list(Path(self.fixture.node.workspace.locator).iterdir()), [])
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_corrupt_prompt_refuses_before_runtime_and_allocation(self):
        with self.assertRaises(ActionWireError):
            self.execute(blob_source=lambda ref: b"wrong prompt")
        self.assertEqual(self.opened, [])
        self.assertEqual(list(self.jobs.iterdir()), [])
        self.assertEqual(self.fixture.generator.calls, [])

    def test_runtime_without_exact_workspace_binding_refuses_before_generation(self):
        @contextmanager
        def runtime(binding):
            yield GenerationWorkerRuntime(
                self.fixture.node.definition,
                self.fixture.node.recipe,
                self.fixture.runner,
            )

        with self.assertRaises(ActionWireError):
            self.execute(runtime_factory=runtime)
        self.assertEqual(self.fixture.generator.calls, [])
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_generation_exception_cleans_owned_stage(self):
        with patch.object(
            self.fixture.generator,
            "generate",
            side_effect=RuntimeError("model failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "model failure"):
                self.execute()
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_authority_loss_after_generation_refuses_and_cleans(self):
        original = self.fixture.generator.generate

        def generate(*args, **kwargs):
            output = original(*args, **kwargs)
            self.healthy = False
            return output

        with patch.object(self.fixture.generator, "generate", side_effect=generate):
            with self.assertRaisesRegex(RuntimeError, "authority changed"):
                self.execute()
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_replaced_stage_is_not_deleted_as_owned_cleanup(self):
        original = self.fixture.generator.generate

        def generate(*args, **kwargs):
            output = original(*args, **kwargs)
            path = self.opened[-1].path
            path.rename(self.fixture.root / "moved-output")
            path.mkdir()
            (path / "foreign").write_text("preserve")
            return output

        with patch.object(self.fixture.generator, "generate", side_effect=generate):
            with self.assertRaises(ActionWireError):
                self.execute()
        self.assertEqual((self.opened[-1].path / "foreign").read_text(), "preserve")

    def test_unrequested_retained_runtime_refuses_before_source_execution(self):
        retained, _ = self.fixture._retained_input()

        @contextmanager
        def runtime(binding):
            with self.runtime(binding) as value:
                value.runner.retained_source = retained
                yield value

        with self.assertRaisesRegex(ActionWireError, "unrequested retained"):
            self.execute(runtime_factory=runtime)
        self.fixture.generator.generate.assert_not_called()
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_retained_request_cannot_use_ordinary_runtime(self):
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
        with self.assertRaisesRegex(ActionWireError, "private retained source differs"):
            self.execute(input_record=raw, input_identity=record_identity(raw))
        self.fixture.generator.generate.assert_not_called()
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_corrupt_retained_transfer_refuses_before_runtime_and_allocation(self):
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

        def fetch(ref):
            if ref == value.prompt:
                return self.fixture.cas.get_bytes(ref)
            return b"corrupt retained file"

        with self.assertRaises(ActionWireError):
            self.execute(
                input_record=raw, input_identity=record_identity(raw), blob_source=fetch
            )
        self.assertEqual(self.opened, [])
        self.assertEqual(list(self.jobs.iterdir()), [])
        self.fixture.generator.generate.assert_not_called()

    def test_retained_worker_materializes_private_input_and_returns_verified_proof(
        self,
    ):
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
        retained.root.rename(retained.root.with_name("original-moved"))
        private_roots = []

        @contextmanager
        def runtime(binding):
            self.assertEqual(list(binding.path.iterdir()), [])
            self.assertNotEqual(binding.retained_source.root, retained.root)
            self.assertEqual(binding.retained_source.identity, retained.identity)
            private_roots.append(binding.retained_source.root)
            with self.runtime(binding) as resolved:
                resolved.runner.retained_source = binding.retained_source
                yield resolved

        result_raw = self.execute(
            input_record=raw,
            input_identity=record_identity(raw),
            runtime_factory=runtime,
        )
        result = GenerateWorkerResult.admit(
            result_raw,
            record_identity(result_raw),
            input_record=raw,
            input_identity=record_identity(raw),
            deadline=self.deadline,
        )
        result.verify_records(
            tuple(
                (ContentIdentity.parse_uri(ref.identity), self.cas.get_bytes(ref))
                for ref in result.evidence_records
            )
        )
        self.assertEqual(
            result.output.provenance.retained_source_identity, retained.identity
        )
        self.assertEqual(result.output.candidate.tree_identity, retained.tree_identity)
        self.fixture.generator.generate.assert_not_called()
        self.assertTrue(private_roots)
        self.assertTrue(all(not path.exists() for path in private_roots))
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_runtime_cannot_change_materialized_retained_input(self):
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

        @contextmanager
        def runtime(binding):
            with self.runtime(binding) as resolved:
                resolved.runner.retained_source = binding.retained_source
                name = binding.retained_source.files[0][0].removeprefix("source/")
                (binding.retained_source.root / name).write_bytes(b"changed")
                yield resolved

        from literate_ai.adapters.retained_source import RetainedSourceError

        with self.assertRaises(RetainedSourceError):
            self.execute(
                input_record=raw,
                input_identity=record_identity(raw),
                runtime_factory=runtime,
            )
        self.fixture.generator.generate.assert_not_called()
        self.assertEqual(list(self.jobs.iterdir()), [])
