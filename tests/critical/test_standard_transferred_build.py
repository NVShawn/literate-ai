"""Transferred build admission cannot publish unvalidated artifact custody."""

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.lifecycle import (
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalStandardLifecycleError,
)
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.contracts import ComponentCommandPhase
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
    rewrite_self_authenticating_artifact,
)


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

    def test_changed_export_and_self_consistent_manifest_are_refused_atomically(self):
        rewrite_self_authenticating_artifact(
            self.artifact, self.output.exports[0].export_id, b"substituted"
        )
        with self.assertRaisesRegex(LocalStandardLifecycleError, "expected evidence"):
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
