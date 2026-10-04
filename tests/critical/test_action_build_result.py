"""Real CAS BUILD result transfers retain proof before local artifact admission."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_build_result import (
    MAX_BUILD_ARCHIVE_BYTES,
    BuildWorkerResult,
    capture_build_result,
    import_build_result,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.directory_artifacts import (
    encode_directory_export,
    read_directory_export,
)
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.storage import FileSystemCAS
from tests.support import fixtures_test_standard_transferred_build as transfer_fixture
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class ActionBuildResultTests(unittest.TestCase):
    def setUp(self):
        fixture = transfer_fixture.StandardTransferredBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.admit()
        self.fixture = fixture
        self.root = fixture.root
        self.worker = fixture.receiver
        self.worker_cas = FileSystemCAS(self.root / "worker-cas")
        self.cas = FileSystemCAS(self.root / "controller-cas")
        custody = self.worker.source_trees.evidence(
            fixture.plan.request.source_tree_identity
        )
        source = self.worker.source_trees.resolve(custody.candidate.tree_identity)
        files = tuple(
            CachedSourceFile(
                path.relative_to(source).as_posix(), self.worker_cas.put_file(path)
            )
            for path in sorted(source.rglob("*"))
            if path.is_file()
        )
        _, execution = _fixture()
        self.input = BuildWorkerInput(
            execution.identity,
            custody.candidate.component_generation_plan_identity,
            custody.candidate,
            fixture.plan,
            fixture.inputs,
            files,
            self.worker.source_trees.validation_inputs(custody.candidate.tree_identity),
            custody.source_generation_identity,
            custody.identity,
            execution.generation_plans[0],
            execution,
        )
        self.input_record = self.input.to_bytes()
        self.input_identity = record_identity(self.input_record)
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        self.controller = LocalStandardLifecyclePorts(
            source_trees=self.worker.source_trees,
            object_root=self.root / "controller",
            contracts=tuple(self.worker.contracts.values()),
            tool_bindings=tuple(self.worker.tool_bindings.values()),
        )
        intent = self.controller.create(
            execution, execution.generation_plans[0], custody.candidate, (), ()
        )
        self.controller.accept_finalized_plan(
            intent, fixture.inputs.authorization, fixture.plan
        )
        self.retained = {}
        self.content = self.capture()
        self.result = BuildWorkerResult.admit(
            self.content,
            record_identity(self.content),
            input_identity=self.input_identity,
            input_record=self.input_record,
            deadline=self.deadline,
        )

    def capture(self):
        return capture_build_result(
            input_record=self.input_record,
            input_identity=self.input_identity,
            deadline=self.deadline,
            ports=self.worker,
            output=self.fixture.output,
            records=self.fixture.recorder.entries,
            cas=self.worker_cas,
        )

    def receive(self, **changes):
        arguments = dict(
            content=self.content,
            result_identity=record_identity(self.content),
            input_record=self.input_record,
            input_identity=self.input_identity,
            deadline=self.deadline,
            ports=self.controller,
            cas=self.cas,
            retain_record=self.retained.__setitem__,
            blob_source=self.worker_cas.get_bytes,
        )
        arguments.update(changes)
        return import_build_result(**arguments)

    def assert_unregistered(self):
        self.assertEqual(self.controller._artifact_paths, {})
        self.assertEqual(list(self.controller.object_root.iterdir()), [])

    def test_corrupt_fetch_never_admits_or_leaves_stage(self):
        with self.assertRaises(ActionWireError):
            self.receive(blob_source=lambda ref: b"corrupt")
        self.assert_unregistered()
        self.assertEqual(self.retained, {})

    def test_valid_hash_for_changed_archive_cannot_replace_build_bytes(self):
        files = read_directory_export(
            self.worker_cas.get_bytes(self.result.artifact_archive),
            self.result.artifact_archive,
            max_bytes=MAX_BUILD_ARCHIVE_BYTES,
            max_entries=65534,
        )
        changed = tuple(
            replace(item, content=b"other") if item.path == "app" else item
            for item in files
        )
        archive = encode_directory_export(
            changed, max_bytes=MAX_BUILD_ARCHIVE_BYTES, max_entries=65534
        )
        result = replace(
            self.result, artifact_archive=self.worker_cas.put_bytes(archive)
        )
        content = result.to_bytes()
        with self.assertRaises(ActionWireError):
            self.receive(content=content, result_identity=record_identity(content))
        self.assert_unregistered()
        self.assertEqual(self.retained, {})
