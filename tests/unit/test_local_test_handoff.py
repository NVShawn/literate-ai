"""Locally completed BUILD feeds isolated TEST without rerunning BUILD."""

import shutil
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_test_execution import execute_worker_test_from_cas
from literate_ai.adapters.action_test_record import TestWorkerResult
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.adapters.local_test_handoff import LocalBuildTestHandoff
from literate_ai.contracts import ComponentCommandPhase
from literate_ai.storage import FileSystemCAS
from tests.support import fixtures_test_command_builder as builder_fixture


class LocalTestHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = builder_fixture.CommandBuilderTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.builder = LocalBuildTestHandoff(f.ports, f.indexer, f.ports)

    def test_actual_local_build_is_tested_from_cas_after_original_paths_are_removed(
        self,
    ):
        f = self.fixture
        with patch.object(f.ports, "build", wraps=f.ports.build) as build:
            output = self.builder.build(f.plan, ())
            value = self.builder.test_handoff(f.plan, output.exports)
            self.assertEqual(build.call_count, 1)
        self.assertFalse(f.marker.exists(), "remote BUILD was dispatched")
        bindings = tuple(f.ports.tool_bindings.values())
        contract = value.build_input.inputs.contract
        source = f.ports.source_trees.resolve(f.plan.request.source_tree_identity)
        shutil.rmtree(source)
        shutil.rmtree(f.ports.object_root)
        workspace = f.root / "isolated-test"
        workspace.mkdir()
        cas = FileSystemCAS(f.root / "isolated-cas")

        @contextmanager
        def factory(admitted, registry, recorder):
            ports = LocalStandardLifecyclePorts(
                source_trees=registry,
                object_root=workspace / "objects",
                contracts=(contract,),
                tool_bindings=bindings,
                command_phases=(ComponentCommandPhase.TEST,),
            )
            ports.retain_evidence_with(recorder)
            with patch.object(
                ports, "build", side_effect=AssertionError("worker BUILD")
            ):
                yield ports

        content = value.to_bytes()
        result = execute_worker_test_from_cas(
            input_record=content,
            input_identity=record_identity(content),
            deadline=f.indexer.deadline,
            cas=cas,
            workspace_root=workspace,
            runtime_factory=factory,
            blob_source=f.indexer.cas.get_bytes,
            owned_workspace=workspace,
        )
        admitted = TestWorkerResult.admit(
            result,
            record_identity(result),
            input_record=content,
            input_identity=record_identity(content),
            deadline=f.indexer.deadline,
        )
        self.assertEqual(admitted.evidence.passed_count, 3)

    def test_failed_local_build_never_publishes_handoff(self):
        f = self.fixture
        with patch.object(f.ports, "build", side_effect=RuntimeError("failed BUILD")):
            with self.assertRaisesRegex(RuntimeError, "failed BUILD"):
                self.builder.build(f.plan, ())
        with self.assertRaises(ActionWireError):
            self.builder.test_handoff(f.plan, f.fixture.fixture.output.exports)

    def test_substituted_exports_refuse_existing_handoff(self):
        f = self.fixture
        self.builder.build(f.plan, ())
        with self.assertRaises(ActionWireError):
            self.builder.test_handoff(f.plan, ())
