"""Live phase admission reaches the production index port and guards results."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
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
from tests.support.fixtures_test_action_blob_source import source_cas_server


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

    def test_live_pool_executes_http_index_and_rechecks_before_recording(self):
        with source_cas_server(self.fixture.blobs) as (url, requests):
            self.configure(url)
            pool = self.pool(source_handoff="http-cas")
            self.assertEqual(len(pool.workers), 1)
            self.assertNotEqual(
                pool.workers[0].observation_identity, self.observed.identity
            )
            recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=1)
            indexer = self.indexer(pool)
            indexer.retain_evidence_with(recorder)
            result = self.fixture.index(indexer)
            self.assertEqual(len(recorder.entries), 1)
            self.assertEqual(result, recorder.entries[0][0])
            self.assertEqual(len(requests), len(self.source.files))
            self.assertEqual(len(self.health_checks), 4)

    def test_stale_hardware_refuses_before_process_probe(self):
        self.configure()
        self.observed = replace(
            self.observed,
            observed_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        )
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities"
        ) as probe:
            with self.assertRaises(ActionWireError):
                self.pool()
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
