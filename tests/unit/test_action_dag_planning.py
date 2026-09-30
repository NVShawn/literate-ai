"""Projection from phase-aware Component plans to exact lifecycle actions."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from literate_ai.application.action_dag_planning import (
    ActionDagPlanningError,
    admit_lifecycle_action_workers,
    lifecycle_action_id,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.execution_dispatch import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.contracts.worker_capabilities import (
    NvidiaProbeStatus,
    WorkerHardwareObservation,
    WorkerHardwareObservationCatalog,
)
from tests.unit.test_component_execution_planning import _diamond_lock, _models


class ActionDagPlanningTests(unittest.TestCase):
    def _plan(self, kind=DependencyKind.GENERATION, *, packaging=False):
        lock = _diamond_lock(
            dependency_kind=kind,
            include_invoice_money_packaging_edge=packaging,
        )
        execution = plan_component_execution(lock, model_identities=_models(lock))
        nodes = plan_lifecycle_action_dag(execution, worker_ids=("beta", "alpha"))
        by_id = {item.action_id: item for item in nodes}
        revisions = {
            item.revision.coordinate.name: item.revision.identity for item in lock.nodes
        }
        return execution, by_id, revisions

    def test_projects_every_component_stage_and_global_tail(self):
        execution, nodes, revisions = self._plan()
        self.assertEqual(len(nodes), len(revisions) * 9 + 2)
        root = execution.root_revision
        package = nodes[lifecycle_action_id(root, LifecycleActionKind.PACKAGE)]
        finalize = nodes[lifecycle_action_id(root, LifecycleActionKind.FINALIZE)]
        self.assertEqual(
            set(package.predecessor_ids),
            {
                lifecycle_action_id(revision, LifecycleActionKind.LINK)
                for revision in revisions.values()
            },
        )
        self.assertEqual(finalize.predecessor_ids, (package.action_id,))
        self.assertTrue(
            all(
                node.eligible_worker_ids == ("alpha", "beta") for node in nodes.values()
            )
        )

    def test_public_interface_generation_does_not_wait_for_provider_execution(self):
        _, nodes, revisions = self._plan()
        pricing_generate = nodes[
            lifecycle_action_id(revisions["pricing"], LifecycleActionKind.GENERATE)
        ]
        self.assertEqual(pricing_generate.predecessor_ids, ())

        pricing_build = nodes[
            lifecycle_action_id(revisions["pricing"], LifecycleActionKind.BUILD)
        ]
        self.assertNotIn(
            lifecycle_action_id(revisions["money"], LifecycleActionKind.BUILD),
            pricing_build.predecessor_ids,
        )

    def test_build_and_toolchain_consumers_wait_for_provider_build_only(self):
        for kind in (DependencyKind.BUILD, DependencyKind.TOOLCHAIN):
            with self.subTest(kind=kind):
                _, nodes, revisions = self._plan(kind)
                pricing_build = nodes[
                    lifecycle_action_id(revisions["pricing"], LifecycleActionKind.BUILD)
                ]
                self.assertIn(
                    lifecycle_action_id(revisions["money"], LifecycleActionKind.BUILD),
                    pricing_build.predecessor_ids,
                )
                self.assertNotIn(
                    lifecycle_action_id(revisions["money"], LifecycleActionKind.ACCEPT),
                    pricing_build.predecessor_ids,
                )

    def test_runtime_consumer_build_overlaps_and_execute_waits_for_provider_build(self):
        _, nodes, revisions = self._plan(DependencyKind.RUNTIME)
        pricing_build = nodes[
            lifecycle_action_id(revisions["pricing"], LifecycleActionKind.BUILD)
        ]
        pricing_execute = nodes[
            lifecycle_action_id(revisions["pricing"], LifecycleActionKind.EXECUTE)
        ]
        provider_build = lifecycle_action_id(
            revisions["money"], LifecycleActionKind.BUILD
        )
        self.assertNotIn(provider_build, pricing_build.predecessor_ids)
        self.assertIn(provider_build, pricing_execute.predecessor_ids)

    def test_package_dependency_waits_at_link_not_generation_or_build(self):
        _, nodes, revisions = self._plan(packaging=True)
        provider_link = lifecycle_action_id(
            revisions["money"], LifecycleActionKind.LINK
        )
        invoice_link = nodes[
            lifecycle_action_id(revisions["invoice-cli"], LifecycleActionKind.LINK)
        ]
        self.assertIn(provider_link, invoice_link.predecessor_ids)
        for kind in (LifecycleActionKind.GENERATE, LifecycleActionKind.BUILD):
            self.assertNotIn(
                provider_link,
                nodes[
                    lifecycle_action_id(revisions["invoice-cli"], kind)
                ].predecessor_ids,
            )

    def test_narrows_eligibility_and_affinity_without_admitting_unknown_workers(self):
        execution, _, revisions = self._plan()
        action_id = lifecycle_action_id(revisions["money"], LifecycleActionKind.BUILD)
        nodes = plan_lifecycle_action_dag(
            execution,
            worker_ids=("alpha", "beta"),
            eligibility={action_id: ("beta",)},
            cache_affinity={action_id: ("beta",)},
        )
        selected = next(item for item in nodes if item.action_id == action_id)
        self.assertEqual(selected.eligible_worker_ids, ("beta",))
        self.assertEqual(selected.cache_affinity_worker_ids, ("beta",))

        with self.assertRaisesRegex(ActionDagPlanningError, "unknown worker"):
            plan_lifecycle_action_dag(
                execution,
                worker_ids=("alpha",),
                eligibility={action_id: ("missing",)},
            )

    def test_admits_only_fresh_compatible_observed_worker_capacity(self):
        catalog = ExecutionWorkerCatalog(
            (
                ExecutionWorker(
                    "fresh",
                    ExecutionWorkerKind.LOCAL,
                    requirements=ExecutionRequirements(os_family="macos"),
                    slots=3,
                ),
                ExecutionWorker(
                    "stale",
                    ExecutionWorkerKind.LOCAL,
                    requirements=ExecutionRequirements(os_family="macos"),
                    slots=2,
                ),
                ExecutionWorker(
                    "wrong-os",
                    ExecutionWorkerKind.LOCAL,
                    requirements=ExecutionRequirements(os_family="linux"),
                ),
            )
        )

        def observation(worker_id: str, observed_at: str):
            return WorkerHardwareObservation(
                worker_id,
                observed_at,
                "macos",
                "macOS",
                "15.0",
                "arm64",
                8,
                8,
                16384,
                (),
                NvidiaProbeStatus.NOT_APPLICABLE,
            )

        observations = WorkerHardwareObservationCatalog(
            (
                observation("fresh", "2026-09-25T11:30:00Z"),
                observation("stale", "2026-09-23T12:00:00Z"),
                observation("wrong-os", "2026-09-25T11:30:00Z"),
            )
        )
        admitted = admit_lifecycle_action_workers(
            catalog,
            observations,
            now=datetime(2026, 9, 25, 12, tzinfo=UTC),
            target_profile="host",
        )
        self.assertEqual(tuple(item.worker_id for item in admitted), ("fresh",))
        self.assertEqual(admitted[0].slots, 3)
        self.assertEqual(admitted[0].catalog_identity, catalog.identity)
        self.assertEqual(
            admitted[0].observation_identity,
            observations.worker("fresh").identity,
        )

    def test_refuses_when_no_current_compatible_worker_exists(self):
        catalog = ExecutionWorkerCatalog(
            (ExecutionWorker("local", ExecutionWorkerKind.LOCAL),)
        )
        with self.assertRaisesRegex(ActionDagPlanningError, "no configured worker"):
            admit_lifecycle_action_workers(
                catalog,
                WorkerHardwareObservationCatalog(()),
                now=datetime(2026, 9, 25, 12, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
