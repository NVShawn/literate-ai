"""Shared test fixtures extracted from test_action_execute_execution."""

import json
import shutil
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from literate_ai.adapters import action_execute_record as records
from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_build_result import (
    BuildWorkerResult,
    capture_build_result,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_execute_execution import (
    execute_worker_execution,
    execute_worker_execution_from_cas,
)
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.adapters.standard_execution_admission import (
    verify_transferred_execution,
)
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.storage import FileSystemCAS
from tests.support import fixtures_test_standard_transferred_build as build_fixture
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class ActionExecuteExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = build_fixture.StandardTransferredBuildTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.admit()
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        self.source_cas = FileSystemCAS(f.root / "source-cas")
        source = f.receiver.source_trees
        custody = source.evidence(f.plan.request.source_tree_identity)
        source_root = source.resolve(custody.candidate.tree_identity)
        files = tuple(
            CachedSourceFile(
                path.relative_to(source_root).as_posix(), self.source_cas.put_file(path)
            )
            for path in sorted(source_root.rglob("*"))
            if path.is_file()
        )
        _, execution = _fixture()
        build_input = BuildWorkerInput(
            execution.identity,
            build_fixture._leaf_generation_plan(execution).identity,
            custody.candidate,
            f.plan,
            f.inputs,
            files,
            source.validation_inputs(custody.candidate.tree_identity),
            custody.source_generation_identity,
            custody.identity,
            build_fixture._leaf_generation_plan(execution),
            execution,
        )
        raw_input = build_input.to_bytes()
        raw_result = capture_build_result(
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=self.deadline,
            ports=f.receiver,
            output=f.output,
            records=f.recorder.entries,
            cas=self.source_cas,
        )
        build_result = BuildWorkerResult.admit(
            raw_result,
            record_identity(raw_result),
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=self.deadline,
        )
        self.value = records.ExecuteWorkerInput(
            build_input,
            build_result,
            plan_standard_execution_receipts(execution, f.plan, f.output.exports, ()),
        )
        self.content = self.value.to_bytes()
        self.identity = record_identity(self.content)
        self.cas = FileSystemCAS(f.root / "test-cas")
        self.ports = LocalStandardLifecyclePorts(
            source_trees=source,
            object_root=f.root / "test-objects",
            contracts=tuple(f.receiver.contracts.values()),
            tool_bindings=tuple(f.receiver.tool_bindings.values()),
            command_phases=(ComponentCommandPhase.EXECUTE,),
        )
        self.ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=64 * 1024 * 1024, max_records=4096)
        )
        shutil.rmtree(f.receiver.object_root)

    def execute(self, **changes):
        arguments = dict(
            input_record=self.content,
            input_identity=self.identity,
            deadline=self.deadline,
            ports=self.ports,
            cas=self.cas,
            blob_source=self.source_cas.get_bytes,
        )
        arguments.update(changes)
        return execute_worker_execution(**arguments)

    def test_execute_only_worker_uses_transferred_build_and_returns_proof(
        self,
    ):
        with (
            patch.object(self.ports, "build", side_effect=AssertionError("rebuild")),
            patch.object(self.ports, "test", side_effect=AssertionError("test")),
        ):
            content = self.execute()
        result = ExecuteWorkerResult.admit(
            content,
            record_identity(content),
            input_record=self.content,
            input_identity=self.identity,
            deadline=self.deadline,
        )
        reader = QualificationEvidenceReader(
            tuple(
                (ContentIdentity.parse_uri(item.identity), self.cas.get_bytes(item))
                for item in result.evidence_records
            ),
            max_bytes=64 * 1024 * 1024,
            max_records=4096,
        )
        verify_transferred_execution(
            reader,
            plan=self.fixture.plan,
            build=self.fixture.output.evidence,
            source_custody=self.ports.source_trees.evidence(
                self.fixture.plan.request.source_tree_identity
            ),
            contract=self.fixture.inputs.contract,
            evidence=result.evidence,
            scope=self.value.scope,
            provider_artifacts=(),
            now=self.ports.clock(),
        )
        self.assertEqual(
            result.evidence.execution_authority.input_scope, self.value.scope
        )
        self.assertEqual(
            self.ports.artifact_path(self.fixture.output.exports[0]).read_text(),
            "known-output\n",
        )
        changed = json.loads(content)
        changed["input_identity"] = canonical_identity("foreign").uri
        with self.assertRaises(ActionWireError):
            bad = canonical_json_bytes(changed)
            ExecuteWorkerResult.admit(
                bad,
                record_identity(bad),
                input_record=self.content,
                input_identity=self.identity,
                deadline=self.deadline,
            )

    def test_closed_input_and_changed_build_binding_refuse_before_fetch_or_execution(
        self,
    ):
        value = json.loads(self.content)
        changed = json.loads(self.content)
        changed["build_result"]["input_identity"] = canonical_identity("foreign").uri
        for content in (
            self.content + b" ",
            canonical_json_bytes(value | {"extra": True}),
            canonical_json_bytes(changed),
            self.content.replace(b'"schema":', b'"extra":1,"extra":2,"schema":', 1),
        ):
            with (
                self.subTest(content=content[:40]),
                patch.object(self.ports, "execute_scoped") as test,
            ):
                fetch = Mock(side_effect=AssertionError("fetch"))
                with self.assertRaises(ActionWireError):
                    self.execute(
                        input_record=content,
                        input_identity=record_identity(content),
                        blob_source=fetch,
                    )
                fetch.assert_not_called()
                test.assert_not_called()

    def test_expired_handoff_refuses_before_fetch_or_execution(self):
        deadline = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        with patch.object(self.ports, "execute_scoped") as test:
            fetch = Mock(side_effect=AssertionError("fetch"))
            with self.assertRaises(ActionWireError):
                self.execute(deadline=deadline, blob_source=fetch)
            fetch.assert_not_called()
            test.assert_not_called()

    def test_corrupt_artifact_refuses_before_execution(self):
        archive = self.value.build_result.artifact_archive

        def fetch(reference):
            return (
                b"corrupt"
                if reference == archive
                else self.source_cas.get_bytes(reference)
            )

        with patch.object(self.ports, "execute_scoped") as test:
            with self.assertRaises(ActionWireError):
                self.execute(blob_source=fetch)
            test.assert_not_called()

    def test_execution_failure_returns_no_result(self):
        with patch.object(
            self.ports, "execute_scoped", side_effect=RuntimeError("test failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "test failed"):
                self.execute()

    def test_result_records_require_unique_bounded_complete_membership(self):
        content = self.execute()
        value = json.loads(content)
        references = value["evidence_records"]
        changed = [
            value | {"extra": True},
            value | {"evidence_records": []},
            value | {"evidence_records": references[::-1]},
            value | {"evidence_records": references + references[:1]},
            value
            | {
                "evidence_records": [
                    item
                    for item in references
                    if item["digest"] != canonical_identity(value["evidence"]).digest
                ]
            },
        ]
        for document in changed:
            with self.subTest(document=document.get("extra")):
                raw = canonical_json_bytes(document)
                with self.assertRaises(ActionWireError):
                    ExecuteWorkerResult.admit(
                        raw,
                        record_identity(raw),
                        input_record=self.content,
                        input_identity=self.identity,
                        deadline=self.deadline,
                    )
        with patch.object(records, "MAX_ACTION_RECORD_BYTES", len(self.content) - 1):
            with self.assertRaises(ActionWireError):
                records.ExecuteWorkerInput.admit(
                    self.content, self.identity, self.deadline
                )

    def test_cas_source_transfer_runs_after_controller_source_removal_and_cleans(self):
        build = self.value.build_input
        source = self.ports.source_trees.resolve(build.candidate.tree_identity)
        shutil.rmtree(source)
        workspace = self.fixture.root / "test-job"
        workspace.mkdir()
        seen = []

        @contextmanager
        def factory(admitted, registry, recorder):
            self.assertEqual(admitted, build)
            materialized = registry.resolve(admitted.candidate.tree_identity)
            seen.append(materialized)
            worker = LocalStandardLifecyclePorts(
                source_trees=registry,
                object_root=workspace / "objects",
                contracts=tuple(self.ports.contracts.values()),
                tool_bindings=tuple(self.ports.tool_bindings.values()),
                command_phases=(ComponentCommandPhase.EXECUTE,),
            )
            worker.retain_evidence_with(recorder)
            try:
                yield worker
            finally:
                shutil.rmtree(worker.object_root)

        content = execute_worker_execution_from_cas(
            input_record=self.content,
            input_identity=self.identity,
            deadline=self.deadline,
            cas=self.cas,
            workspace_root=workspace,
            runtime_factory=factory,
            blob_source=self.source_cas.get_bytes,
            owned_workspace=workspace,
        )
        result = ExecuteWorkerResult.admit(
            content,
            record_identity(content),
            input_record=self.content,
            input_identity=self.identity,
            deadline=self.deadline,
        )
        self.assertEqual(
            result.evidence.execution_authority.input_scope, self.value.scope
        )
        self.assertFalse(seen[0].exists())
        self.assertEqual(list(workspace.iterdir()), [])
