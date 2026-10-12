"""Shared test fixtures extracted from test_action_admission."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorkerCatalog,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.worker_capabilities import (
    NvidiaProbeStatus,
    WorkerHardwareObservation,
    WorkerHardwareObservationCatalog,
)
from tests.support import fixtures_test_command_indexer as index_fixture
from tests.support.action_deadline import ACTION_TEST_DEADLINE


class CommandActionAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = index_fixture.CommandIndexerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source = self.fixture.fixture
        self.source.deadline = ActionDispatchDeadline(
            datetime.now(UTC) + ACTION_TEST_DEADLINE
        )
        self.observed = WorkerHardwareObservation(
            "index",
            datetime.now(UTC).isoformat(),
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
        self.health_checks = []
        self.extra_observations = ()

    def health(self, worker):
        self.health_checks.append(worker.identity)
        return canonical_identity({"healthy": worker.identity.uri})

    def configure(self, url=None):
        if url:
            self.source.bind_remote_source(url)
        worker = replace(
            self.source.worker, action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL
        )
        self.source.worker = worker
        self.catalog = ExecutionWorkerCatalog((worker,))

    def pool(self, **changes):
        arguments = dict(
            phase=LifecycleActionKind.INDEX,
            source_handoff="filesystem-cas",
            target_profile="host",
            cwd=self.source.root,
            environment={
                **os.environ,
                "INDEX_WORKER_IDENTITY": self.source.worker.identity.uri,
            },
        )
        arguments.update(changes)
        return CommandActionWorkerPool(
            lambda: self.catalog,
            lambda: WorkerHardwareObservationCatalog(
                tuple(
                    sorted(
                        (self.observed, *self.extra_observations),
                        key=lambda item: item.worker_id,
                    )
                )
            ),
            self.health,
            self.source.deadline,
            **arguments,
        )

    def indexer(self, pool):
        return CommandGenerationIndexer.from_admission(
            self.source.execution_plan,
            self.fixture.registry,
            lambda _source: self.source.candidate,
            self.fixture.controller_cas,
            pool,
        )

    def test_package_requires_exact_packager_and_profile_drift_revokes_admission(self):
        from literate_ai.adapters.action_capabilities import package_profile_identity

        self.configure()
        pool = self.pool()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        packager = canonical_identity("packager")
        self.assertFalse(pool.supports_package(worker, packager))
        configured = replace(
            original,
            actions=tuple(sorted((*original.actions, LifecycleActionKind.PACKAGE))),
            package_profile=package_profile_identity(packager),
        )
        pool._facts[worker.worker_id] = configured
        self.assertTrue(pool.supports_package(worker, packager))
        self.assertFalse(
            pool.supports_package(worker, canonical_identity("other-packager"))
        )
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities",
            return_value=replace(
                configured,
                package_profile=package_profile_identity(canonical_identity("changed")),
            ),
        ):
            with self.assertRaises(ActionWireError) as error:
                pool.revalidate(worker)
            self.assertEqual(error.exception.code, "action_admission.runtime_changed")

    def test_finalize_requires_exact_profile_and_profile_drift_revokes_admission(self):
        self.configure()
        pool = self.pool()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        packager = canonical_identity("packager")
        self.assertFalse(pool.supports_finalize(worker, packager))
        configured = replace(
            original,
            actions=tuple(sorted((*original.actions, LifecycleActionKind.FINALIZE))),
            finalize_profile=packager,
        )
        pool._facts[worker.worker_id] = configured
        self.assertTrue(pool.supports_finalize(worker, packager))
        self.assertFalse(
            pool.supports_finalize(worker, canonical_identity("other-packager"))
        )
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities",
            return_value=replace(
                configured,
                finalize_profile=canonical_identity("changed"),
            ),
        ):
            with self.assertRaises(ActionWireError) as error:
                pool.revalidate(worker)
            self.assertEqual(error.exception.code, "action_admission.runtime_changed")


if __name__ == "__main__":
    unittest.main()
