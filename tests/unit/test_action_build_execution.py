"""BUILD execution refuses missing evidence or changed custody before host work."""

import unittest
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.action_build_execution import (
    execute_worker_build,
    execute_worker_build_from_cas,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.contracts import canonical_identity
from tests.support import fixtures_test_action_build_source as source_fixture
from tests.support.fixtures_test_action_build_record import build_worker_input


class ActionBuildExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.ActionBuildSourceTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.value = build_worker_input(self.fixture)

    def execute(self):
        content = self.value.to_bytes()
        return execute_worker_build(
            input_record=content,
            input_identity=record_identity(content),
            deadline=self.fixture.deadline,
            ports=self.fixture.ports,
            cas=self.fixture.cas,
        )

    def test_missing_recorder_refuses_before_build(self):
        with patch.object(self.fixture.ports, "build") as build:
            with self.assertRaisesRegex(LocalStandardLifecycleError, "capture"):
                self.execute()
            build.assert_not_called()

    def test_foreign_source_custody_refuses_before_build(self):
        self.fixture.ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=64 * 1024 * 1024, max_records=4096)
        )
        self.value = replace(
            self.value, source_custody_identity=canonical_identity("foreign")
        )
        with patch.object(self.fixture.ports, "build") as build:
            with self.assertRaises(ActionWireError) as raised:
                self.execute()
            self.assertEqual(raised.exception.code, "action_build.source_invalid")
            build.assert_not_called()

    def test_changed_runtime_plan_inputs_refuse_before_build(self):
        self.fixture.ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=64 * 1024 * 1024, max_records=4096)
        )
        with (
            patch.object(
                self.fixture.ports,
                "plan_finalization_inputs",
                return_value=replace(self.value.inputs, dependency_resolution="python"),
            ),
            patch.object(self.fixture.ports, "build") as build,
        ):
            with self.assertRaises(ActionWireError) as raised:
                self.execute()
            self.assertEqual(raised.exception.code, "action_build.input_invalid")
            build.assert_not_called()

    def test_source_context_cleans_after_runtime_entry_or_execution_failure(self):
        for during_entry in (True, False):
            with self.subTest(during_entry=during_entry):
                observed = []

                @contextmanager
                def runtime_factory(
                    admitted,
                    registry,
                    recorder,
                    *,
                    observed=observed,
                    during_entry=during_entry,
                ):
                    source = registry.resolve(admitted.candidate.tree_identity)
                    self.assertTrue(source.is_dir())
                    observed.append(source)
                    if during_entry:
                        raise RuntimeError("private runtime unavailable")
                    try:
                        yield self.fixture.ports
                    finally:
                        observed.append("runtime closed")

                content = self.value.to_bytes()
                with patch(
                    "literate_ai.adapters.action_build_execution.execute_worker_build",
                    side_effect=RuntimeError("build failed"),
                ) as execute:
                    with self.assertRaises(RuntimeError):
                        execute_worker_build_from_cas(
                            input_record=content,
                            input_identity=record_identity(content),
                            deadline=self.fixture.deadline,
                            cas=self.fixture.cas,
                            workspace_root=self.fixture.workspace,
                            blob_source=self.fixture.controller_cas.get_bytes,
                            runtime_factory=runtime_factory,
                        )
                    self.assertEqual(execute.call_count, 0 if during_entry else 1)
                self.assertFalse(observed[0].exists())
                self.assertEqual(list(self.fixture.workspace.iterdir()), [])
                if not during_entry:
                    self.assertEqual(observed[-1], "runtime closed")

    def test_owned_workspace_refuses_external_or_root_artifacts_before_build(self):
        for object_root in (self.fixture.root, self.fixture.workspace):
            with self.subTest(object_root=object_root):
                closed = []

                @contextmanager
                def runtime_factory(
                    admitted,
                    registry,
                    recorder,
                    *,
                    object_root=object_root,
                    closed=closed,
                ):
                    try:
                        yield SimpleNamespace(object_root=object_root)
                    finally:
                        closed.append(True)

                content = self.value.to_bytes()
                with patch(
                    "literate_ai.adapters.action_build_execution.execute_worker_build"
                ) as execute:
                    with self.assertRaises(ActionWireError) as raised:
                        execute_worker_build_from_cas(
                            input_record=content,
                            input_identity=record_identity(content),
                            deadline=self.fixture.deadline,
                            cas=self.fixture.cas,
                            workspace_root=self.fixture.workspace,
                            owned_workspace=self.fixture.workspace,
                            blob_source=self.fixture.controller_cas.get_bytes,
                            runtime_factory=runtime_factory,
                        )
                    self.assertEqual(
                        raised.exception.code, "action_build.workspace_invalid"
                    )
                    execute.assert_not_called()
                self.assertEqual(closed, [True])
                self.assertEqual(list(self.fixture.workspace.iterdir()), [])
