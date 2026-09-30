"""Public Standard project application-service tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.generation_preparation import (
    FilesystemComponentWorkspaceAllocator,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.application.component_execution_planning import (
    authored_assets_from_lock,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardProjectLifecycleService,
)
from literate_ai.application.standard_project_services import (
    PreparedExecutableProject,
    StandardProjectApplicationService,
    StandardProjectApplicationServiceError,
)
from tests.unit.test_component_execution_planning import (
    _diamond_lock_with_locked_money_asset,
    _models,
)
from tests.unit.test_component_generation_scheduling import _decision, _names
from tests.unit.test_component_node_generation_preparation import _budget, _fixture
from tests.unit.test_standard_project_lifecycle import (
    LifecyclePorts,
    _ContextEvidenceRecorder,
    _prepared_execution,
    _prepared_nodes,
)


class _Publisher:
    def publish(self, membership):
        return membership.identity


def _lifecycle(ports: LifecyclePorts) -> StandardProjectLifecycleService:
    return StandardProjectLifecycleService(
        validator=ports,
        build_intent_factory=ports,
        build_plan_finalizer=ports,
        generator=ports,
        indexer=ports,
        authorizer=ports,
        builder=ports,
        tester=ports,
        executor=ports,
        acceptor=ports,
        source_cache_publisher=_Publisher(),
        artifact_assembler=ports,
        package_creator=ports,
        root_integration_tester=ports,
        packaged_project_executor=ports,
        independent_project_acceptor=ports,
        admitter=ports,
        receipt_issuer=ports,
        context_evidence_recorder=_ContextEvidenceRecorder(),
        clock=lambda: datetime(2026, 8, 7, 0, 1, tzinfo=UTC),
    )


class StandardProjectApplicationServiceTests(unittest.TestCase):
    def test_plan_and_prepare_preserve_exact_locked_execution(self) -> None:
        snapshot, expected_execution = _fixture()
        model_identities = {
            plan.component_revision.uri: plan.generation_key.model_identity
            for plan in expected_execution.generation_plans
        }

        execution = StandardProjectApplicationService.plan(
            snapshot.authority.lock,
            model_identities=model_identities,
        )
        self.assertEqual(execution, expected_execution)
        self.assertEqual(
            execution,
            StandardProjectApplicationService.plan(
                snapshot.authority.lock,
                model_identities=model_identities,
                assets=authored_assets_from_lock(snapshot.authority.lock),
            ),
        )

        adapter = LockedComponentNodePreparationAdapter()
        with tempfile.TemporaryDirectory() as directory:
            allocator = FilesystemComponentWorkspaceAllocator(Path(directory))
            prepared = StandardProjectApplicationService.prepare(
                execution,
                authority=snapshot,
                authority_lock_identity=adapter.authority_lock_identity,
                authority_guard=adapter.guard,
                node_projector=adapter.project,
                workspace_allocator=allocator.allocate,
                framework_envelope=lambda projection: projection.recipe.prompt().encode(
                    "utf-8"
                ),
                budget=_budget(),
            )

        self.assertEqual(prepared.execution_plan, execution)
        self.assertEqual(
            tuple(prepared.nodes_by_revision),
            tuple(plan.component_revision.uri for plan in execution.generation_plans),
        )
        self.assertEqual(
            prepared.identity,
            PreparedExecutableProject(execution, prepared.nodes).identity,
        )

    def test_omitted_plan_binds_locked_asset_identities(self) -> None:
        lock = _diamond_lock_with_locked_money_asset()
        bound = authored_assets_from_lock(lock)
        self.assertEqual(len(bound), 1)
        omitted = StandardProjectApplicationService.plan(
            lock, model_identities=_models(lock)
        )
        self.assertEqual(
            omitted,
            StandardProjectApplicationService.plan(
                lock, model_identities=_models(lock), assets=bound
            ),
        )
        money_plan = next(
            item
            for item in omitted.generation_plans
            if item.component_revision == bound[0].component_revision
        )
        self.assertEqual(
            money_plan.generation_key.asset_identities, (bound[0].identity,)
        )
        empty = StandardProjectApplicationService.plan(
            lock, model_identities=_models(lock), assets=()
        )
        empty_money = next(
            item
            for item in empty.generation_plans
            if item.component_revision == bound[0].component_revision
        )
        self.assertEqual(empty_money.generation_key.asset_identities, ())

    def test_prepared_project_rejects_missing_or_reordered_nodes(self) -> None:
        lock_snapshot, _ = _fixture()
        execution, requests = _prepared_execution(lock_snapshot.authority.lock)
        nodes = _prepared_nodes(execution, requests)
        ordered = tuple(
            nodes[plan.component_revision.uri] for plan in execution.generation_plans
        )

        for invalid in (ordered[:-1], tuple(reversed(ordered))):
            with self.subTest(size=len(invalid)):
                with self.assertRaises(StandardProjectApplicationServiceError) as error:
                    PreparedExecutableProject(execution, invalid)
                self.assertEqual(
                    error.exception.code, "project_service.preparation_incomplete"
                )
        with self.assertRaisesRegex(TypeError, "nodes must be a tuple"):
            PreparedExecutableProject(execution, list(ordered))  # type: ignore[arg-type]

    def test_rebuild_delegates_complete_prepared_project_to_lifecycle(self) -> None:
        snapshot, _ = _fixture()
        lock = snapshot.authority.lock
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)
        ports = LifecyclePorts(execution, names)
        service = StandardProjectApplicationService(_lifecycle(ports))
        prepared = PreparedExecutableProject(
            execution,
            tuple(
                nodes[plan.component_revision.uri]
                for plan in execution.generation_plans
            ),
        )

        result = service.rebuild(
            prepared,
            component_lock=lock,
            invalidation=_decision(
                execution,
                names,
                next(iter(names.values())),
                tuple(names.values()),
            ),
            max_parallelism=2,
        )

        self.assertTrue(result.successful)
        self.assertEqual(
            ports.events[-2:], [("admit", "project"), ("receipt", "project")]
        )


if __name__ == "__main__":
    unittest.main()
