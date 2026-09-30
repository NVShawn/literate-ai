"""Cross-instance source restoration and retry-lineage tests."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.cache import FilesystemStandardLifecycleCheckpointStore
from literate_ai.adapters.lifecycle import local_generated_source_tree_identity
from literate_ai.adapters.standard_project import (
    PlannedStandardProject,
    assemble_filesystem_standard_project_runtime,
)
from literate_ai.contracts import (
    ContentIdentity,
    GeneratedSourceCandidate,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    StandardLifecycleCheckpointOutcome,
    StandardLifecycleStage,
    StandardLifecycleStageEvidence,
    canonical_identity,
)
from tests.unit.test_component_node_generation_preparation import _budget, _fixture
from tests.unit.test_standard_project_factory import (
    _command_contracts,
    _selection,
    _toolchain_closure,
)


def _stage(prepared, execution, output, stage, *, failed=False):
    recipe_identity = prepared.recipe.identity
    if isinstance(recipe_identity, str):
        recipe_identity = ContentIdentity.parse_uri(recipe_identity)
    resume = SourceGenerationResumeCandidate(
        output,
        output.identity,
        prepared.request.request.budget.identity,
        prepared.request.request.complexity_decision_identity,
    )
    return StandardLifecycleStageEvidence(
        execution.identity,
        prepared.plan.component_revision,
        prepared.plan.identity,
        prepared.plan.generation_key.identity,
        recipe_identity,
        resume,
        stage,
        canonical_identity({"checkpoint-subject": stage.value}),
        (
            StandardLifecycleCheckpointOutcome.FAILED
            if failed
            else StandardLifecycleCheckpointOutcome.PASSED
        ),
        "test.failed" if failed else None,
    )


class _Registry:
    def __init__(self):
        self.paths = {}

    def register(self, candidate, root, **_kwargs):
        self.paths[candidate.tree_identity.uri] = Path(root)


def _output(prepared, execution):
    root = Path(prepared.workspace.locator)
    source = root / "source" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('checkpoint')\n", encoding="utf-8")
    tree = local_generated_source_tree_identity(root)
    request = prepared.request.request
    recipe_identity = prepared.recipe.identity
    if isinstance(recipe_identity, str):
        recipe_identity = ContentIdentity.parse_uri(recipe_identity)
    candidate = GeneratedSourceCandidate(
        prepared.plan.component_revision,
        request.identity,
        canonical_identity({"planned-request": tree.uri}),
        prepared.plan.identity,
        prepared.plan.generation_key.identity,
        request.context_manifest_identity,
        request.prompt_identity,
        recipe_identity,
        prepared.workspace.allocation_identity,
        tree,
        canonical_identity({"bundle": tree.uri}),
        canonical_identity({"manifest": tree.uri}),
        canonical_identity({"bom": tree.uri}),
        canonical_identity({"tests": tree.uri}),
    )
    provenance = SourceGenerationProvenance(
        request.identity,
        candidate.planned_coding_cli_request_identity,
        execution.component_lock_identity,
        execution.root_revision,
        prepared.plan.component_revision,
        prepared.plan.identity,
        prepared.plan.generation_key.identity,
        request.context_manifest_identity,
        request.prompt_identity,
        recipe_identity,
        prepared.workspace.allocation_identity,
        canonical_identity({"readiness": tree.uri}),
        (canonical_identity({"route": tree.uri}),),
        (canonical_identity({"model": tree.uri}),),
        candidate.identity,
    )
    return SourceGenerationRunOutput(
        candidate, candidate.identity, provenance, provenance.identity
    )


class StandardLifecycleCheckpointStoreTests(unittest.TestCase):
    def test_retained_checkpoint_never_restores_as_generated_source(self):
        snapshot, execution = _fixture()
        contracts, tools = _command_contracts(execution)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _node: None,
                object_root=root / "objects",
                toolchain_closure=_toolchain_closure(execution, contracts, tools),
            )
            first = runtime.prepare(
                snapshot,
                PlannedStandardProject(_selection(), execution),
                source_root=root / "first",
                budget=_budget(),
            )
            node = first.nodes[0]
            output = _output(node, execution)
            provenance = replace(
                output.provenance,
                route_decision_identities=(),
                model_stage_output_identities=(),
                retained_source_identity=canonical_identity({"retained": 1}),
            )
            output = replace(
                output, provenance=provenance, provenance_identity=provenance.identity
            )
            store = FilesystemStandardLifecycleCheckpointStore(
                root / "checkpoints",
                source_root=lambda _identity: Path(node.workspace.locator),
                source_trees=_Registry(),
            )
            store.start_attempt(execution, first.nodes_by_revision)
            store.record(
                _stage(
                    node, execution, output, StandardLifecycleStage.SOURCE_GENERATION
                )
            )
            second = runtime.prepare(
                snapshot,
                PlannedStandardProject(_selection(), execution),
                source_root=root / "second",
                budget=_budget(),
            )
            reopened = FilesystemStandardLifecycleCheckpointStore(
                root / "checkpoints",
                source_root=lambda _identity: Path(node.workspace.locator),
                source_trees=_Registry(),
            )
            self.assertIsNone(reopened.restore_prepared(execution, second.nodes[0]))
            self.assertEqual(
                list(Path(second.nodes[0].workspace.locator).iterdir()), []
            )

    def test_source_capture_uses_portable_wire_path_order(self) -> None:
        snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        closure = _toolchain_closure(execution, contracts, tool_bindings)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _node: None,
                object_root=root / "objects",
                toolchain_closure=closure,
            )
            prepared_project = runtime.prepare(
                snapshot,
                PlannedStandardProject(_selection(), execution),
                source_root=root / "sources",
                budget=_budget(),
            )
            prepared = prepared_project.nodes[0]
            root = Path(prepared.workspace.locator)
            output = _output(prepared, execution)
            (root / "source" / "Z.py").write_text("pass\n", encoding="utf-8")
            (root / "source" / "a.py").write_text("pass\n", encoding="utf-8")
            evidence = _stage(
                prepared,
                execution,
                output,
                StandardLifecycleStage.SOURCE_GENERATION,
            )

            store = FilesystemStandardLifecycleCheckpointStore(
                root / "checkpoints",
                source_root=lambda _identity: Path(prepared.workspace.locator),
                source_trees=_Registry(),
            )
            captured = store._capture_source_files(evidence)

        paths = tuple(item.path for item in captured)
        self.assertEqual(paths, tuple(sorted(paths)))

    def test_new_store_restores_source_and_appends_retry_lineage(self) -> None:
        snapshot, execution = _fixture()
        contracts, tool_bindings = _command_contracts(execution)
        closure = _toolchain_closure(execution, contracts, tool_bindings)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _node: None,
                object_root=root / "objects",
                toolchain_closure=closure,
            )
            first_prepared = first_runtime.prepare(
                snapshot,
                PlannedStandardProject(_selection(), execution),
                source_root=root / "first-sources",
                budget=_budget(),
            )
            first_node = first_prepared.nodes[0]
            output = _output(first_node, execution)
            first_registry = _Registry()
            first_registry.register(
                output.candidate, Path(first_node.workspace.locator)
            )
            store = FilesystemStandardLifecycleCheckpointStore(
                root / "checkpoints",
                source_root=lambda _identity: Path(first_node.workspace.locator),
                source_trees=first_registry,
            )
            store.start_attempt(execution, first_prepared.nodes_by_revision)
            store.record(
                _stage(
                    first_node,
                    execution,
                    output,
                    StandardLifecycleStage.SOURCE_GENERATION,
                )
            )
            store.record(
                _stage(
                    first_node,
                    execution,
                    output,
                    StandardLifecycleStage.BUILD_PLAN,
                )
            )
            store.record(
                _stage(
                    first_node,
                    execution,
                    output,
                    StandardLifecycleStage.TEST,
                    failed=True,
                )
            )
            first_head = store.latest(
                execution.identity, first_node.plan.component_revision
            )
            assert first_head is not None

            second_runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _node: (_ for _ in ()).throw(
                    AssertionError("restoration must not invoke generation")
                ),
                object_root=root / "objects",
                toolchain_closure=closure,
            )
            second_prepared = second_runtime.prepare(
                snapshot,
                PlannedStandardProject(_selection(), execution),
                source_root=root / "second-sources",
                budget=_budget(),
            )
            second_node = second_prepared.nodes[0]
            second_registry = _Registry()
            reopened = FilesystemStandardLifecycleCheckpointStore(
                root / "checkpoints",
                source_root=lambda _identity: Path(second_node.workspace.locator),
                source_trees=second_registry,
            )
            restored = reopened.restore_prepared(execution, second_node)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(
                restored.output.candidate.tree_identity,
                output.candidate.tree_identity,
            )
            self.assertEqual(
                restored.output.candidate.workspace_allocation_identity,
                second_node.workspace.allocation_identity,
            )
            self.assertNotEqual(restored.output.identity, output.identity)
            self.assertTrue(
                (Path(second_node.workspace.locator) / "source" / "main.py").is_file()
            )

            reopened.start_attempt(execution, second_prepared.nodes_by_revision)
            reopened.record(
                _stage(
                    second_node,
                    execution,
                    restored.output,
                    StandardLifecycleStage.SOURCE_GENERATION,
                )
            )
            second_head = reopened.latest(
                execution.identity, second_node.plan.component_revision
            )
            second_attempt = reopened.latest_attempt(
                execution.identity, second_node.plan.component_revision
            )
            assert second_head is not None
            assert second_attempt is not None
            self.assertEqual(second_head.attempt, 2)
            self.assertEqual(second_head.attempt_evidence, second_attempt)
            self.assertEqual(second_head.sequence, first_head.sequence + 1)
            self.assertEqual(second_head.predecessor_identity, first_head.identity)

            # Starting a third attempt is itself durable. A process may stop before
            # producing source, and the fourth process must still retain that retry.
            reopened.start_attempt(execution, second_prepared.nodes_by_revision)
            third_attempt = reopened.latest_attempt(
                execution.identity, second_node.plan.component_revision
            )
            assert third_attempt is not None
            self.assertEqual(third_attempt.attempt, 3)
            another = FilesystemStandardLifecycleCheckpointStore(
                root / "checkpoints",
                source_root=lambda _identity: Path(second_node.workspace.locator),
                source_trees=second_registry,
            )
            another.start_attempt(execution, second_prepared.nodes_by_revision)
            fourth_attempt = another.latest_attempt(
                execution.identity, second_node.plan.component_revision
            )
            assert fourth_attempt is not None
            self.assertEqual(fourth_attempt.attempt, 4)
            self.assertEqual(
                fourth_attempt.predecessor_attempt_identity, third_attempt.identity
            )
            self.assertEqual(
                fourth_attempt.source_predecessor_identity, second_head.identity
            )


if __name__ == "__main__":
    unittest.main()
