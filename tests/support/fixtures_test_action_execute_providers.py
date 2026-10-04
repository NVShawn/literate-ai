"""Shared fixtures extracted from ``tests.unit.test_action_execute_providers``."""

import shutil

import unittest

from contextlib import contextmanager

from dataclasses import replace

from unittest.mock import patch

from literate_ai.adapters.action_build_record import BuildWorkerInput

from literate_ai.adapters.action_build_result import (
    BuildWorkerResult,
    capture_build_result,
)

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity

from literate_ai.adapters.action_execute_execution import (
    execute_worker_execution_from_cas,
)

from literate_ai.adapters.action_execute_record import ExecuteWorkerInput

from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult

from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts

from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder

from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)

from literate_ai.contracts import ComponentCommandPhase

from literate_ai.contracts.capabilities import DependencyKind

from literate_ai.contracts.generation_cache import CachedSourceFile

from tests.support import fixtures_test_action_provider_build as provider_fixture

from tests.unit.standard_source_evidence_fixture import register_strict_source

from tests.support.fixtures_test_component_node_generation_preparation import _fixture

class ExecuteProviderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = provider_fixture.ProviderBuildTransferTests()
        self.addCleanup(f.doCleanups)
        snapshot, execution = _fixture(dependency_kind=DependencyKind.RUNTIME)
        with (
            patch(
                "tests.unit.test_standard_local_command_adapter._fixture",
                return_value=(snapshot, execution),
            ),
            patch(
                "tests.unit.test_standard_transferred_build._fixture",
                return_value=(snapshot, execution),
            ),
            patch(
                "tests.unit.test_action_provider_build._fixture",
                return_value=(snapshot, execution),
            ),
        ):
            f.setUp()
        self.provider = f.receipt
        consumer_revision = next(
            edge.consumer_revision
            for action in execution.action_plans
            for edge in action.dependency_edges
            if edge.provider_revision == self.provider.component_revision
        )
        generation = next(
            item
            for item in execution.generation_plans
            if item.component_revision == consumer_revision
        )
        original = f.fixture.receiver
        provider_contract = original.contracts[self.provider.component_revision.uri]
        command = replace(
            provider_contract.command(ComponentCommandPhase.EXECUTE),
            argv=(
                "{tool}",
                "-c",
                "from pathlib import Path; import os,sys; "
                "assert (Path(sys.argv[1])/'app').read_text().strip()=='consumer'; "
                "print(Path(os.environ['PROVIDER']).read_text().strip())",
                "{artifact_root}",
            ),
        )
        contract = replace(
            provider_contract,
            component_revision=generation.component_revision,
            commands=tuple(
                command if item.phase is ComponentCommandPhase.EXECUTE else item
                for item in provider_contract.commands
            ),
        )
        self.contracts = (provider_contract, contract)
        self.bindings = tuple(original.tool_bindings.values())
        consumer = LocalStandardLifecyclePorts(
            source_trees=original.source_trees,
            object_root=f.fixture.root / "consumer",
            contracts=self.contracts,
            tool_bindings=self.bindings,
        )
        recorder = QualificationEvidenceRecorder(
            max_bytes=64 * 1024 * 1024, max_records=4096
        )
        consumer.retain_evidence_with(recorder)
        source = f.fixture.root / "consumer-source"
        source.mkdir()
        (source / "app.py").write_text("consumer\n")
        candidate = register_strict_source(
            consumer.source_trees,
            source,
            snapshot=snapshot,
            generation_plan=generation,
            identity_namespace="execute-runtime-provider",
        )
        intent = consumer.create(execution, generation, candidate, (), ())
        authorization = consumer.authorize(
            intent,
            consumer.index(candidate.component_revision, candidate.tree_identity),
        )
        inputs = consumer.plan_finalization_inputs(intent, authorization)
        self.plan = plan = consumer.finalize(intent, authorization)
        output = consumer.build(plan, ())
        self.assertEqual(plan.provider_artifact_identities, ())
        self.assertEqual(output.exports[0].dependency_artifact_identities, ())
        custody = consumer.source_trees.evidence(candidate.tree_identity)
        build = BuildWorkerInput(
            execution.identity,
            generation.identity,
            candidate,
            plan,
            inputs,
            tuple(
                CachedSourceFile(
                    path.relative_to(source).as_posix(), f.source.put_file(path)
                )
                for path in sorted(source.rglob("*"))
                if path.is_file()
            ),
            consumer.source_trees.validation_inputs(candidate.tree_identity),
            custody.source_generation_identity,
            custody.identity,
            generation,
            execution,
        )
        raw_input = build.to_bytes()
        raw_result = capture_build_result(
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=f.deadline,
            ports=consumer,
            output=output,
            records=recorder.entries,
            cas=f.source,
        )
        result = BuildWorkerResult.admit(
            raw_result,
            record_identity(raw_result),
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=f.deadline,
        )
        scope = plan_standard_execution_receipts(
            execution, plan, output.exports, (self.provider,)
        )
        self.assertTrue(scope.runtime_dependencies)
        self.value = ExecuteWorkerInput(
            build, result, scope, (self.provider,), (f.transfer,)
        )
        self.content = self.value.to_bytes()
        self.workspace = f.fixture.root / "execute-job"
        self.workspace.mkdir()
        self.workers = []
        self.consumer = consumer
        if not getattr(self, "preserve_sources", False):
            shutil.rmtree(source)
            shutil.rmtree(consumer.object_root)
            shutil.rmtree(f.archive_root)

    def execute(self, *, fail=False, blob_source=None):
        @contextmanager
        def runtime(build, registry, recorder):
            ports = LocalStandardLifecyclePorts(
                source_trees=registry,
                object_root=self.workspace / "objects",
                contracts=self.contracts,
                tool_bindings=self.bindings,
                provider_environment={"app": ("PROVIDER", "app")},
                command_phases=(ComponentCommandPhase.EXECUTE,),
            )
            ports.retain_evidence_with(recorder)
            self.workers.append(ports)
            with patch.object(ports, "build", side_effect=AssertionError("rebuild")):
                try:
                    if fail:
                        with patch.object(
                            ports,
                            "execute_scoped",
                            side_effect=RuntimeError("execute failed"),
                        ):
                            yield ports
                    else:
                        yield ports
                finally:
                    shutil.rmtree(ports.object_root)

        return execute_worker_execution_from_cas(
            input_record=self.content,
            input_identity=record_identity(self.content),
            deadline=self.fixture.deadline,
            cas=self.fixture.target,
            workspace_root=self.workspace,
            runtime_factory=runtime,
            blob_source=blob_source or self.fixture.source.get_bytes,
            owned_workspace=self.workspace,
        )

    def test_configured_receiver_checks_runtime_archive_before_job_allocation(self):
        import os
        import sys

        from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
        from literate_ai.adapters.lifecycle import LocalComponentToolBinding
        from tests.support.fixtures_test_action_execute_action import make_execute_request

        request, records = make_execute_request(self.value, self.fixture.deadline)
        worker = ConfiguredExecuteWorker(
            LocalComponentToolBinding(sys.executable, ("-c", "raise SystemExit(2)")),
            self.bindings,
            environment=dict(os.environ),
        )

        def fetch(reference):
            if reference == self.fixture.transfer.artifact_archive:
                return b"corrupt"
            return self.fixture.source.get_bytes(reference)

        with (
            patch(
                "literate_ai.adapters.action_execute_worker.tempfile.mkdtemp",
                side_effect=AssertionError("allocated before runtime verification"),
            ),
            self.assertRaises(ActionWireError),
        ):
            worker.execute(
                request,
                self.fixture.deadline,
                records,
                expected_worker_identity=request.worker.worker_identity,
                cas=self.fixture.target,
                workspace_root=self.workspace,
                blob_source=fetch,
            )
        self.assert_clean()

    def assert_clean(self):
        self.assertEqual(list(self.workspace.iterdir()), [])
        for ports in self.workers:
            for export in self.provider.build.exports:
                self.assertNotIn(export.identity.uri, ports._exports_by_identity)
                self.assertNotIn(export.identity.uri, ports._artifact_paths)

    def test_actual_runtime_provider_survives_original_removal_without_rebuild(self):
        content = self.execute()
        result = ExecuteWorkerResult.admit(
            content,
            record_identity(content),
            input_record=self.content,
            input_identity=record_identity(self.content),
            deadline=self.fixture.deadline,
        )
        self.assertEqual(
            self.workers[-1].execution_stdout[self.plan.component_revision.uri],
            "known-output",
        )
        self.assertEqual(
            result.evidence.provider_artifact_identities,
            self.value.scope.provider_artifact_identities,
        )
        self.assertEqual(self.plan.provider_artifact_identities, ())
        self.assert_clean()

    def test_runtime_failure_cleans_provider_registration_and_workspace(self):
        with self.assertRaisesRegex(RuntimeError, "execute failed"):
            self.execute(fail=True)
        self.assert_clean()

    def test_corrupt_runtime_archive_refuses_before_execution(self):
        def fetch(reference):
            if reference == self.fixture.transfer.artifact_archive:
                return b"corrupt"
            return self.fixture.source.get_bytes(reference)

        with self.assertRaises(ActionWireError):
            self.execute(blob_source=fetch)
        self.assertFalse(self.workers[-1]._execution_evidence)
        self.assert_clean()

