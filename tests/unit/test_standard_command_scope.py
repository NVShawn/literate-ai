"""Local phase scopes retain actual dependencies and refuse unrelated execution."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.lifecycle import (
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.lifecycle.standard_bazel import StandardBazelLifecyclePorts
from literate_ai.adapters.lifecycle.standard_cargo import StandardCargoLifecyclePorts
from literate_ai.contracts import ComponentCommandPhase, ComponentCommandToolBinding
from tests.support import fixtures_test_standard_npm_lifecycle as npm_tests
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
)


class StandardCommandScopeTests(unittest.TestCase):
    def test_specialized_adapters_preserve_scope_and_full_phase_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, _, _, _ = _python_copy_lifecycle(root)
            for adapter, targets in (
                (StandardBazelLifecyclePorts, {"bazel_targets": ()}),
                (StandardCargoLifecyclePorts, {"cargo_targets": ()}),
            ):
                arguments = dict(
                    source_trees=original.source_trees,
                    object_root=root / adapter.__name__,
                    contracts=tuple(original.contracts.values()),
                    tool_bindings=tuple(original.tool_bindings.values()),
                    **targets,
                )
                with self.subTest(adapter=adapter.__name__):
                    adapter(**arguments)
                    ports = adapter(
                        **arguments, command_phases=(ComponentCommandPhase.BUILD,)
                    )
                    for operation in (ports.test, ports.execute):
                        with self.assertRaisesRegex(
                            LocalStandardLifecycleError, "scope"
                        ):
                            operation(None, ())
                    with self.assertRaisesRegex(ValueError, "every and only"):
                        adapter(**arguments, command_phases=())
                    data = adapter(
                        **{**arguments, "tool_bindings": ()}, command_phases=()
                    )
                    self.assertFalse(data.locked_command_authority_is_current())
                    for operation in (data.build, data.test, data.execute):
                        with self.assertRaisesRegex(
                            LocalStandardLifecycleError, "scope"
                        ):
                            operation(None, ())

    def test_build_with_unavailable_test_and_execution_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, _, candidate, _ = _python_copy_lifecycle(root)
            contract = next(iter(original.contracts.values()))
            contract = replace(
                contract,
                tool_bindings=(
                    contract.tool_binding(ComponentCommandPhase.BUILD),
                    ComponentCommandToolBinding(
                        ComponentCommandPhase.TEST, _identity("remote-test-tool")
                    ),
                    ComponentCommandToolBinding(
                        ComponentCommandPhase.EXECUTE, _identity("remote-execute-tool")
                    ),
                ),
            )
            arguments = dict(
                source_trees=original.source_trees,
                object_root=root / "worker",
                contracts=(contract,),
                tool_bindings=tuple(original.tool_bindings.values()),
            )
            with self.assertRaisesRegex(ValueError, "every and only"):
                LocalStandardLifecyclePorts(**arguments)
            ports = LocalStandardLifecyclePorts(
                **arguments, command_phases=(ComponentCommandPhase.BUILD,)
            )
            _, execution = _fixture()
            intent = ports.create(
                execution, execution.generation_plans[0], candidate, (), ()
            )
            plan = ports.finalize(
                intent,
                ports.authorize(
                    intent,
                    ports.index(candidate.component_revision, candidate.tree_identity),
                ),
            )
            result = ports.build(plan, ())
            self.assertTrue(result)
            self.assertEqual(
                ports.artifact_path(result.exports[0]).read_text(),
                "known-output\n",
            )

    def test_out_of_scope_calls_refuse_before_custody_or_process_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, _, _, _ = _python_copy_lifecycle(root)
            ports = LocalStandardLifecyclePorts(
                source_trees=original.source_trees,
                object_root=root / "worker",
                contracts=tuple(original.contracts.values()),
                tool_bindings=tuple(original.tool_bindings.values()),
                command_phases=(ComponentCommandPhase.BUILD,),
            )
            calls = [
                lambda: ports.test(None, ()),
                lambda: ports.execute(None, ()),
                lambda: ports.execute_scoped(None, (), None, ()),
                lambda: ports.execution_command(None, ()),
                lambda: ports._run_locked(
                    None,
                    ComponentCommandPhase.EXECUTE,
                    source_root=None,
                    object_root=None,
                    artifact_root=None,
                    export_path=None,
                    providers=(),
                ),
                lambda: ports._run_packaged(None, ComponentCommandPhase.TEST),
                lambda: ports.test_root_integration(None, None, None, None, None),
                lambda: ports.execute_packaged_project(None, None, None, None, None),
                lambda: ports.accept_project_independently(
                    None, None, None, None, None, None, None
                ),
            ]
            with (
                mock.patch.object(
                    ports.source_trees,
                    "resolve",
                    side_effect=AssertionError("custody accessed"),
                ),
                mock.patch(
                    "subprocess.run", side_effect=AssertionError("process launched")
                ),
            ):
                for call in calls:
                    with (
                        self.subTest(call=call),
                        self.assertRaisesRegex(LocalStandardLifecycleError, "scope"),
                    ):
                        call()
            self.assertEqual(list((root / "worker").iterdir()), [])

    def test_invalid_scope_refuses_before_creating_object_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for phases in (
                ("build",),
                [ComponentCommandPhase.BUILD],
                (ComponentCommandPhase.BUILD,) * 2,
                (ComponentCommandPhase.TEST, ComponentCommandPhase.BUILD),
            ):
                with (
                    self.subTest(phases=phases),
                    self.assertRaisesRegex(ValueError, "canonical"),
                ):
                    LocalStandardLifecyclePorts(
                        source_trees=None,
                        object_root=root / "objects",
                        contracts=(),
                        command_phases=phases,
                    )
            self.assertFalse((root / "objects").exists())

    def test_test_only_runtime_refuses_build_even_with_shared_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, _, _, _ = _python_copy_lifecycle(root)
            ports = LocalStandardLifecyclePorts(
                source_trees=original.source_trees,
                object_root=root / "worker",
                contracts=tuple(original.contracts.values()),
                tool_bindings=tuple(original.tool_bindings.values()),
                command_phases=(ComponentCommandPhase.TEST,),
            )
            with self.assertRaisesRegex(LocalStandardLifecycleError, "build.*scope"):
                ports.build(None, ())

    def test_npm_build_still_requires_node_but_test_does_not_require_npm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, _, _, _, target = npm_tests.StandardNpmLifecycleTests()._fixture(
                root
            )
            arguments = dict(
                source_trees=original.source_trees,
                object_root=root / "worker",
                contracts=tuple(original.contracts.values()),
                npm_targets=(target,),
            )
            npm = original.tool_bindings[target.build_system_toolchain_identity.uri]
            node = original.tool_bindings[target.node_toolchain_identity.uri]
            with self.assertRaisesRegex(ValueError, "every and only"):
                LocalStandardLifecyclePorts(
                    **arguments,
                    tool_bindings=(npm,),
                    command_phases=(ComponentCommandPhase.BUILD,),
                )
            LocalStandardLifecyclePorts(
                **arguments,
                tool_bindings=(npm, node),
                command_phases=(ComponentCommandPhase.BUILD,),
            )
            LocalStandardLifecyclePorts(
                **arguments,
                tool_bindings=(node,),
                command_phases=(ComponentCommandPhase.TEST,),
            )


class StandardCustodyOnlyTests(unittest.TestCase):
    def test_transferred_artifact_admission_needs_no_local_execution_tools(self):
        import shutil

        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceRecorder,
        )
        from tests.support import fixtures_test_standard_transferred_build as transferred

        fixture = transferred.StandardTransferredBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        original = fixture.producer
        receiver = LocalStandardLifecyclePorts(
            source_trees=original.source_trees,
            object_root=fixture.root / "data-only",
            contracts=tuple(original.contracts.values()),
            command_phases=(),
        )
        receiver.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=5000000, max_records=1000)
        )
        candidate = original.source_trees.evidence(
            fixture.plan.request.source_tree_identity
        ).candidate
        _, execution = _fixture()
        artifact = receiver.object_root / "transferred"
        shutil.copytree(fixture.artifact, artifact)
        shutil.rmtree(fixture.artifact)
        with (
            mock.patch(
                "subprocess.run", side_effect=AssertionError("process launched")
            ),
            mock.patch(
                "subprocess.Popen", side_effect=AssertionError("process launched")
            ),
            mock.patch(
                "literate_ai.adapters.lifecycle.LocalComponentToolBinding.require_unchanged",
                side_effect=AssertionError("local tool inspected"),
            ),
        ):
            intent = receiver.create(
                execution, execution.generation_plans[0], candidate, (), ()
            )
            receiver.accept_finalized_plan(
                intent, fixture.inputs.authorization, fixture.plan
            )
            output = receiver.admit_transferred_build(
                plan=fixture.plan,
                inputs=fixture.inputs,
                evidence=fixture.output.evidence,
                evidence_reader=fixture.reader,
                artifact=artifact,
            )
            self.assertEqual(output.evidence, fixture.output.evidence)
            self.assertEqual(
                receiver.artifact_path(output.exports[0]).read_bytes(),
                b"known-output\n",
            )
            self.assertFalse(receiver.tool_bindings)
            self.assertFalse(receiver.locked_command_authority_is_current())
            for operation in (receiver.build, receiver.test, receiver.execute):
                with self.assertRaisesRegex(LocalStandardLifecycleError, "scope"):
                    operation(fixture.plan, output.exports)
            with self.assertRaisesRegex(LocalStandardLifecycleError, "scope"):
                receiver.accept_project_independently(
                    None, None, None, None, None, None, None
                )
