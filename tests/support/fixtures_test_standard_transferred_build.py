"""Shared test fixtures extracted from test_standard_transferred_build."""

import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_build_result import (
    capture_build_result,
    import_build_result,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.dependencies import CycloneDxBomError
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.lifecycle.standard_local import (
    _RESOLVED_SBOM,
    LocalStandardLifecycleError,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.contracts import ComponentCommandPhase, ComponentCommandToolBinding
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.security import AuthorizationError
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
    rewrite_self_authenticating_artifact,
)
from tests.unit.action_deadline import ACTION_TEST_DEADLINE


class StandardTransferredBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        producer_root = self.root / "producer"
        producer_root.mkdir()
        self.producer, _, candidate, intent = _python_copy_lifecycle(producer_root)
        contract = next(iter(self.producer.contracts.values()))
        test_command = contract.command(ComponentCommandPhase.TEST)
        script = (
            "from pathlib import Path; import json,sys; "
            "assert (Path(sys.argv[1])/'app').read_text().strip()=='known-output'; "
            "print(json.dumps(dict(schema='literate-ai/generated-test-results@1',"
            "cases=[dict(case_id=case,outcome='passed') for case in "
            "('fixture-example','fixture-boundary','fixture-invariant')])))"
        )
        test_command = replace(
            test_command, argv=("{tool}", "-c", script, "{artifact_root}")
        )
        contract = replace(
            contract,
            commands=tuple(
                test_command if command.phase is ComponentCommandPhase.TEST else command
                for command in contract.commands
            ),
        )
        self.producer = LocalStandardLifecyclePorts(
            source_trees=self.producer.source_trees,
            object_root=producer_root / "tested-objects",
            contracts=(contract,),
            tool_bindings=tuple(self.producer.tool_bindings.values()),
        )
        _, execution = _fixture()
        intent = self.producer.create(
            execution, execution.generation_plans[0], candidate, (), ()
        )
        authorization = self.producer.authorize(intent, _identity("index"))
        self.inputs = self.producer.plan_finalization_inputs(intent, authorization)
        self.plan = self.producer.finalize(intent, authorization)
        self.recorder = QualificationEvidenceRecorder(
            max_bytes=5_000_000, max_records=1000
        )
        self.producer.retain_evidence_with(self.recorder)
        self.output = self.producer.build(self.plan, ())
        self.reader = QualificationEvidenceReader(
            self.recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        self.receiver = LocalStandardLifecyclePorts(
            source_trees=self.producer.source_trees,
            object_root=self.root / "receiver",
            contracts=tuple(self.producer.contracts.values()),
            tool_bindings=tuple(self.producer.tool_bindings.values()),
        )
        _, execution = _fixture()
        received_intent = self.receiver.create(
            execution, execution.generation_plans[0], candidate, (), ()
        )
        self.receiver.accept_finalized_plan(received_intent, authorization, self.plan)
        self.artifact = self.receiver.object_root / "transferred"
        producer_artifact = self.producer.artifact_path(self.output.exports[0]).parent
        shutil.copytree(producer_artifact, self.artifact)
        shutil.rmtree(producer_artifact)

    def admit(self, **changes):
        arguments = dict(
            plan=self.plan,
            inputs=self.inputs,
            evidence=self.output.evidence,
            evidence_reader=self.reader,
            artifact=self.artifact,
        )
        arguments.update(changes)
        return self.receiver.admit_transferred_build(**arguments)

    def assert_unregistered(self):
        for name in (
            "_artifact_paths",
            "_artifact_blob_paths",
            "_artifact_blob_bytes",
            "_exports_by_identity",
            "_planned_exports",
            "_build_observations",
            "_build_evidence",
        ):
            self.assertEqual(getattr(self.receiver, name), {}, name)
        with self.assertRaises(LocalStandardLifecycleError):
            self.receiver.artifact_path(self.output.exports[0])

    def test_transfer_preserves_exact_evidence_after_producer_artifact_removal(self):
        output = self.admit()
        self.assertEqual(output, self.output)
        self.assertEqual(
            self.receiver.artifact_path(output.exports[0]).read_text(), "known-output\n"
        )
        self.assertEqual(
            self.receiver.resolved_sbom_content(output.evidence),
            (self.artifact / _RESOLVED_SBOM).read_bytes(),
        )
        self.assertEqual(self.admit(), output)
        tests = self.receiver.test(self.plan, output.exports)
        self.assertEqual(tests.component_revision, self.plan.component_revision)

    def test_changed_export_and_self_consistent_manifest_are_refused_atomically(self):
        rewrite_self_authenticating_artifact(
            self.artifact, self.output.exports[0].export_id, b"substituted"
        )
        with self.assertRaisesRegex(LocalStandardLifecycleError, "expected evidence"):
            self.admit()
        self.assert_unregistered()

    def test_changed_expected_evidence_cannot_admit_original_bytes(self):
        evidence = replace(
            self.output.evidence, build_observation_identity=_identity("other-build")
        )
        with self.assertRaises(QualificationCaptureError):
            self.admit(evidence=evidence)
        self.assert_unregistered()

    def test_expired_grant_or_unretained_plan_refuses_before_artifact_read(self):
        self.receiver.clock = lambda: self.inputs.authorization.grant.expires_at
        with patch.object(self.receiver, "_cached_build_output") as read:
            with self.assertRaises(AuthorizationError):
                self.admit()
            read.assert_not_called()
        self.assert_unregistered()
        self.receiver.clock = self.producer.clock
        self.receiver._plans_by_revision.clear()
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "retained local authority"
        ):
            self.admit()
        self.assert_unregistered()

    def test_ordinary_build_validation_failure_does_not_register_exports(self):
        (self.artifact / _RESOLVED_SBOM).write_bytes(b"invalid")
        with self.assertRaises(CycloneDxBomError):
            self.receiver._build_output(self.plan, self.artifact)
        self.assert_unregistered()

    def test_expiry_or_artifact_change_during_validation_prevents_registration(self):
        original = self.receiver._record_evidence
        for mode in ("expiry", "tree"):
            with self.subTest(mode=mode):
                self.receiver.clock = self.producer.clock
                extra = self.artifact / "late-file"
                extra.unlink(missing_ok=True)

                def record(document, mode=mode, extra=extra):
                    identity = original(document)
                    if mode == "expiry":
                        self.receiver.clock = lambda: (
                            self.inputs.authorization.grant.expires_at
                        )
                    else:
                        extra.write_bytes(b"changed during admission")
                    return identity

                with patch.object(
                    self.receiver, "_record_evidence", side_effect=record
                ):
                    with self.assertRaises(
                        (AuthorizationError, LocalStandardLifecycleError)
                    ):
                        self.admit()
                self.assert_unregistered()

    def test_foreign_or_redirected_artifact_root_is_refused(self):
        foreign = self.root / "foreign"
        shutil.copytree(self.artifact, foreign)
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "outside object custody"
        ):
            self.admit(artifact=foreign)
        self.assert_unregistered()

    def test_failed_replacement_preserves_prior_admitted_custody(self):
        self.admit()
        before = dict(self.receiver._artifact_paths)
        replacement = self.receiver.object_root / "replacement"
        shutil.copytree(self.artifact, replacement)
        rewrite_self_authenticating_artifact(
            replacement, self.output.exports[0].export_id, b"changed"
        )
        with self.assertRaises(LocalStandardLifecycleError):
            self.admit(artifact=replacement)
        self.assertEqual(self.receiver._artifact_paths, before)
        self.assertEqual(
            self.receiver.artifact_path(self.output.exports[0]).read_text(),
            "known-output\n",
        )

    def test_redirected_file_is_refused_without_registration(self):
        foreign = self.root / "outside"
        foreign.write_bytes(b"foreign")
        redirected = self.artifact / "redirected"
        try:
            redirected.symlink_to(foreign)
        except OSError as exc:
            self.skipTest(f"host cannot create symlinks: {type(exc).__name__}")
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "cannot contain links"
        ):
            self.admit()
        self.assert_unregistered()

    def test_missing_supporting_evidence_refuses_before_artifact_access(self):
        for identity in (
            self.output.evidence.identity,
            self.output.evidence.build_observation_identity,
            self.output.evidence.artifact_custody_identity,
        ):
            with self.subTest(identity=identity):
                reader = QualificationEvidenceReader(
                    tuple(
                        item for item in self.recorder.entries if item[0] != identity
                    ),
                    max_bytes=5_000_000,
                    max_records=1000,
                )
                with patch.object(self.receiver, "_cached_build_output") as reopen:
                    with self.assertRaises(QualificationCaptureError):
                        self.admit(evidence_reader=reader)
                    reopen.assert_not_called()
                self.assert_unregistered()

    def test_expiry_during_final_tree_check_still_prevents_registration(self):
        from literate_ai.adapters.lifecycle import standard_local

        original = standard_local.local_tree_identity
        calls = 0

        def tree(root):
            nonlocal calls
            calls += 1
            identity = original(root)
            if calls == 2:
                self.receiver.clock = lambda: self.inputs.authorization.grant.expires_at
            return identity

        with patch.object(standard_local, "local_tree_identity", side_effect=tree):
            with self.assertRaises(AuthorizationError):
                self.admit()
        self.assertEqual(calls, 2)
        self.assert_unregistered()

    def test_test_only_receiver_admits_without_the_build_tool_binding(self):
        build_tool = LocalComponentToolBinding(sys.executable, ("-I",))
        test_tools = tuple(self.producer.tool_bindings.values())
        contract = replace(
            self.inputs.contract,
            language_compiler_identity=build_tool.toolchain_identity,
            tool_bindings=tuple(
                ComponentCommandToolBinding(
                    binding.phase, build_tool.toolchain_identity
                )
                if binding.phase is ComponentCommandPhase.BUILD
                else binding
                for binding in self.inputs.contract.tool_bindings
            ),
        )
        source = self.producer.source_trees
        candidate = source.evidence(self.plan.request.source_tree_identity).candidate
        producer = LocalStandardLifecyclePorts(
            source_trees=source,
            object_root=self.root / "scoped-producer",
            contracts=(contract,),
            tool_bindings=(build_tool, *test_tools),
        )
        _, execution = _fixture()
        intent = producer.create(
            execution, execution.generation_plans[0], candidate, (), ()
        )
        authorization = producer.authorize(
            intent,
            producer.index(candidate.component_revision, candidate.tree_identity),
        )
        inputs = producer.plan_finalization_inputs(intent, authorization)
        plan = producer.finalize(intent, authorization)
        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        producer.retain_evidence_with(recorder)
        built = producer.build(plan, ())
        receiver = LocalStandardLifecyclePorts(
            source_trees=source,
            object_root=self.root / "test-only",
            contracts=(contract,),
            tool_bindings=test_tools,
            command_phases=(ComponentCommandPhase.TEST,),
        )
        self.assertNotIn(build_tool.toolchain_identity.uri, receiver.tool_bindings)
        received_intent = receiver.create(
            execution, execution.generation_plans[0], candidate, (), ()
        )
        receiver.accept_finalized_plan(received_intent, authorization, plan)
        worker_cas = FileSystemCAS(self.root / "scoped-worker-cas")
        source_root = source.resolve(candidate.tree_identity)
        custody = source.evidence(candidate.tree_identity)
        files = tuple(
            CachedSourceFile(
                path.relative_to(source_root).as_posix(), worker_cas.put_file(path)
            )
            for path in sorted(source_root.rglob("*"))
            if path.is_file()
        )
        input_record = BuildWorkerInput(
            execution.identity,
            execution.generation_plans[0].identity,
            candidate,
            plan,
            inputs,
            files,
            source.validation_inputs(candidate.tree_identity),
            custody.source_generation_identity,
            custody.identity,
            execution.generation_plans[0],
            execution,
        ).to_bytes()
        input_identity = record_identity(input_record)
        deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        result_record = capture_build_result(
            input_record=input_record,
            input_identity=input_identity,
            deadline=deadline,
            ports=producer,
            output=built,
            records=recorder.entries,
            cas=worker_cas,
        )
        shutil.rmtree(producer.object_root)
        retained = {}
        admitted = import_build_result(
            content=result_record,
            result_identity=record_identity(result_record),
            input_record=input_record,
            input_identity=input_identity,
            deadline=deadline,
            ports=receiver,
            cas=FileSystemCAS(self.root / "scoped-test-cas"),
            blob_source=worker_cas.get_bytes,
            retain_record=retained.__setitem__,
        )
        self.assertEqual(retained[record_identity(result_record)], result_record)
        self.assertEqual(admitted, built)
        tested = receiver.test(plan, admitted.exports)
        self.assertEqual(tested.component_revision, plan.component_revision)
        with patch.object(
            receiver, "_build_locked", side_effect=AssertionError("compiler executed")
        ) as run:
            with self.assertRaisesRegex(LocalStandardLifecycleError, "scope"):
                receiver.build(plan, ())
            run.assert_not_called()
