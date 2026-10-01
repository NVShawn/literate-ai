"""Transferred source retains the same recipe-derived evidence checks."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.dependencies import CycloneDxBomError
from literate_ai.adapters.generation_preparation import (
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.source_evidence_validation import (
    SourceEvidenceValidationInputs,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    canonical_identity,
    canonical_json_bytes,
)
from tests.unit.test_component_node_generation_preparation import _fixture
from tests.unit.test_standard_local_command_adapter import _python_copy_lifecycle


class StandardSourceValidationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        controller = self.root / "controller"
        controller.mkdir()
        self.ports, _, self.candidate, _ = _python_copy_lifecycle(controller)
        self.custody = self.ports.source_trees.evidence(self.candidate.tree_identity)
        self.snapshot, self.execution = _fixture()
        self.generation = self.execution.generation_plans[0]
        self.recipe = (
            LockedComponentNodePreparationAdapter()
            .project(self.snapshot, self.generation)
            .recipe
        )
        self.inputs = self.ports.source_trees.validation_inputs(
            self.candidate.tree_identity
        )
        self.assertEqual(
            self.inputs, SourceEvidenceValidationInputs.from_recipe(self.recipe)
        )
        self.worker = self.root / "worker-source"
        shutil.copytree(
            self.ports.source_trees.resolve(self.candidate.tree_identity), self.worker
        )
        self.registry = LocalSourceTreeRegistry()

    def register(self, inputs):
        self.registry.register(
            self.candidate,
            self.worker,
            source_generation_identity=self.custody.source_generation_identity,
            validation_inputs=inputs,
        )

    def test_transferred_inputs_retain_custody_and_build_without_controller_source(
        self,
    ):
        received = SourceEvidenceValidationInputs.from_dict(
            json.loads(canonical_json_bytes(self.inputs.to_dict()))
        )
        self.assertEqual(received.identity, self.inputs.identity)
        shutil.rmtree(self.root / "controller")
        self.register(received)
        self.assertEqual(
            self.registry.evidence(self.candidate.tree_identity), self.custody
        )
        worker = LocalStandardLifecyclePorts(
            source_trees=self.registry,
            object_root=self.root / "objects",
            contracts=tuple(self.ports.contracts.values()),
            tool_bindings=tuple(self.ports.tool_bindings.values()),
            command_phases=(ComponentCommandPhase.BUILD,),
        )
        intent = worker.create(self.execution, self.generation, self.candidate, (), ())
        plan = worker.finalize(
            intent,
            worker.authorize(
                intent,
                worker.index(
                    self.candidate.component_revision, self.candidate.tree_identity
                ),
            ),
        )
        output = worker.build(plan, ())
        self.assertEqual(
            worker.artifact_path(output.exports[0]).read_text(), "known-output\n"
        )

    def test_changed_validation_authority_refuses_without_registration(self):
        for mutate in (
            lambda value: value.update(recipe_identity="sha256:" + "f" * 64),
            lambda value: value.update(specification_references=["foreign.md"]),
            lambda value: value["managed_graph"].update(
                resolved_graph_identity=canonical_identity("foreign-graph").to_dict()
            ),
            lambda value: value["acceptance"].update(result_shape={"other": "string"}),
        ):
            with self.subTest(mutate=mutate):
                value = self.inputs.to_dict()
                mutate(value)
                with self.assertRaises((ValueError, CycloneDxBomError)):
                    self.register(SourceEvidenceValidationInputs.from_dict(value))
                with self.assertRaisesRegex(
                    LocalStandardLifecycleError, "not registered"
                ):
                    self.registry.registered_root(self.candidate.tree_identity)

    def test_changed_worker_bytes_refuse_before_retaining_custody(self):
        (self.worker / "app.py").write_text("changed")
        with self.assertRaisesRegex(LocalStandardLifecycleError, "candidate identity"):
            self.register(self.inputs)
        with self.assertRaisesRegex(LocalStandardLifecycleError, "no strict evidence"):
            self.registry.registered_evidence(self.candidate.tree_identity)

    def test_document_is_closed_bounded_and_does_not_share_mutable_acceptance(self):
        document = self.inputs.to_dict()
        received = SourceEvidenceValidationInputs.from_dict(document)
        identity = received.identity
        document["acceptance"]["arguments"].append(["changed"])
        self.assertEqual(received.identity, identity)
        for value in (
            {**self.inputs.to_dict(), "extra": True},
            {**self.inputs.to_dict(), "schema": "unknown"},
            {
                **self.inputs.to_dict(),
                "specification_references": ["x" * (16 * 1024 * 1024)],
            },
        ):
            with self.assertRaisesRegex(ValueError, "invalid portable"):
                SourceEvidenceValidationInputs.from_dict(value)

    def test_recipe_and_transferred_inputs_cannot_compete(self):
        with self.assertRaisesRegex(LocalStandardLifecycleError, "ambiguous"):
            self.registry.register(
                self.candidate,
                self.worker,
                recipe=self.recipe,
                validation_inputs=self.inputs,
            )
        with self.assertRaisesRegex(LocalStandardLifecycleError, "not registered"):
            self.registry.registered_root(self.candidate.tree_identity)

    def test_reregistration_cannot_replace_validation_authority(self):
        self.register(self.inputs)
        document = self.inputs.to_dict()
        document["acceptance"]["result_shape"] = None
        changed = SourceEvidenceValidationInputs.from_dict(document)
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "competing validation"
        ):
            self.register(changed)
        self.assertEqual(
            self.registry.validation_inputs(self.candidate.tree_identity), self.inputs
        )
        self.assertEqual(
            self.registry.evidence(self.candidate.tree_identity), self.custody
        )
