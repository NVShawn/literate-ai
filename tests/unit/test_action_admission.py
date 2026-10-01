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
from tests.unit import test_command_indexer as index_fixture
from tests.unit.test_action_blob_source import source_cas_server


class CommandActionAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = index_fixture.CommandIndexerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source = self.fixture.fixture
        self.source.deadline = ActionDispatchDeadline(
            datetime.now(UTC) + timedelta(minutes=5)
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

    def test_route_change_refuses_before_source_publication(self):
        self.configure()
        pool = self.pool()
        self.catalog = ExecutionWorkerCatalog((replace(self.source.worker, slots=2),))
        with self.assertRaisesRegex(ActionWireError, "catalog changed"):
            self.fixture.index(self.indexer(pool))
        self.assertTrue(all(not path.exists() for path in self.fixture.blobs.values()))

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

    def test_legacy_worker_is_not_probed(self):
        self.configure()
        self.catalog = ExecutionWorkerCatalog(
            (replace(self.source.worker, action_protocol=None),)
        )
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities"
        ) as probe:
            with self.assertRaises(ActionWireError):
                self.pool()
            probe.assert_not_called()

    def test_actual_receiver_rejects_unsupported_phase_or_handoff(self):
        self.configure()
        for changes in (
            {"phase": LifecycleActionKind.BUILD},
            {"source_handoff": "http-cas"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ActionWireError):
                self.pool(**changes)

    def test_missing_health_evidence_does_not_invoke_worker(self):
        self.configure()
        self.health = lambda _worker: None
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities"
        ) as probe:
            with self.assertRaises(ActionWireError):
                self.pool()
            probe.assert_not_called()

    def test_runtime_drift_refuses_an_existing_admission(self):
        self.configure()
        pool = self.pool()
        actual = pool.capabilities[0]
        with patch.object(
            pool,
            "_probe",
            return_value=replace(
                actual, python_identity=canonical_identity("other-python")
            ),
        ):
            with self.assertRaisesRegex(ActionWireError, "runtime"):
                pool.revalidate(pool.workers[0])

    def test_unavailable_worker_is_excluded_without_exposing_private_reason(self):
        self.configure()
        unavailable = replace(self.source.worker, worker_id="bad")
        self.catalog = ExecutionWorkerCatalog((unavailable, self.source.worker))
        self.extra_observations = (replace(self.observed, worker_id="bad"),)
        original = self.health

        def health(worker):
            if worker.worker_id == "bad":
                raise ActionWireError("private.failure", "private policy detail")
            return original(worker)

        self.health = health
        pool = self.pool()
        self.assertEqual(tuple(item.worker_id for item in pool.workers), ("index",))
        self.assertEqual(pool.refusals, (("bad", "action_admission.unavailable"),))
        self.assertEqual(pool.workers[0].catalog_identity, self.catalog.identity)

    def test_hardware_change_invalidates_admitted_slots_before_another_probe(self):
        self.configure()
        pool = self.pool()
        self.observed = replace(self.observed, memory_mib=32768)
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities"
        ) as probe:
            with self.assertRaisesRegex(ActionWireError, "hardware observation"):
                pool.revalidate(pool.workers[0])
            probe.assert_not_called()

    def test_health_rejection_after_execution_does_not_retain_result(self):
        with source_cas_server(self.fixture.blobs) as (url, _requests):
            self.configure(url)
            pool = self.pool(source_handoff="http-cas")
            original = pool.health_admission

            def health(worker):
                if len(self.health_checks) == 3:
                    raise ActionWireError("health.hold", "health holds result")
                return original(worker)

            pool.health_admission = health
            recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=1)
            indexer = self.indexer(pool)
            indexer.retain_evidence_with(recorder)
            with self.assertRaisesRegex(ActionWireError, "health holds"):
                self.fixture.index(indexer)
            self.assertEqual(recorder.entries, ())

    def test_fresh_hardware_timestamp_preserves_admission_but_stale_refuses(self):
        self.configure()
        pool = self.pool()
        initial = pool.identity
        self.observed = replace(
            self.observed, observed_at=datetime.now(UTC).isoformat()
        )
        pool.revalidate(pool.workers[0])
        self.assertEqual(pool.identity, initial)
        self.assertEqual(pool.hardware_observations.worker("index"), self.observed)
        self.observed = replace(
            self.observed,
            observed_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        )
        with self.assertRaises(ActionWireError):
            pool.revalidate(pool.workers[0])

    def test_catalog_change_during_hardware_collection_refuses_initial_admission(self):
        self.configure()
        original = self.catalog
        changed = ExecutionWorkerCatalog((replace(self.source.worker, slots=2),))
        with patch.object(CommandActionWorkerPool, "_probe") as probe:
            with self.assertRaisesRegex(ActionWireError, "catalog changed"):
                CommandActionWorkerPool(
                    iter((original, changed)).__next__,
                    lambda: WorkerHardwareObservationCatalog((self.observed,)),
                    self.health,
                    self.source.deadline,
                    phase=LifecycleActionKind.INDEX,
                    source_handoff="filesystem-cas",
                    target_profile="host",
                    cwd=self.source.root,
                )
            probe.assert_not_called()

    def test_build_requires_exact_inventory_and_profile_drift_invalidates_admission(
        self,
    ):
        self.configure()
        pool = self.pool()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        tool = canonical_identity("compiler")
        self.assertFalse(pool.supports_build(worker, (tool,)))
        configured = replace(
            original,
            actions=tuple(sorted((*original.actions, LifecycleActionKind.BUILD))),
            build_profile=canonical_identity("startup"),
            build_toolchains=(tool,),
        )
        pool._facts[worker.worker_id] = configured
        self.assertTrue(pool.supports_build(worker, (tool,)))
        self.assertFalse(pool.supports_build(worker, (canonical_identity("other"),)))
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities",
            return_value=replace(
                configured, build_profile=canonical_identity("changed")
            ),
        ):
            with self.assertRaises(ActionWireError) as raised:
                pool.revalidate(worker)
            self.assertEqual(raised.exception.code, "action_admission.runtime_changed")

    def test_test_requires_exact_inventory_and_profile_drift_invalidates_admission(
        self,
    ):
        self.configure()
        pool = self.pool()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        tool = canonical_identity("runner")
        self.assertFalse(pool.supports_test(worker, (tool,)))
        configured = replace(
            original,
            actions=tuple(sorted((*original.actions, LifecycleActionKind.TEST))),
            test_profile=canonical_identity("startup"),
            test_toolchains=(tool,),
        )
        pool._facts[worker.worker_id] = configured
        self.assertTrue(pool.supports_test(worker, (tool,)))
        self.assertFalse(pool.supports_test(worker, (canonical_identity("other"),)))
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities",
            return_value=replace(
                configured, test_profile=canonical_identity("changed")
            ),
        ):
            with self.assertRaises(ActionWireError) as raised:
                pool.revalidate(worker)
            self.assertEqual(raised.exception.code, "action_admission.runtime_changed")

    def test_execute_requires_exact_inventory_and_profile_drift_invalidates_admission(
        self,
    ):
        self.configure()
        pool = self.pool()
        worker = pool.workers[0]
        original = pool._facts[worker.worker_id]
        tool = canonical_identity("runtime")
        self.assertFalse(pool.supports_execute(worker, (tool,)))
        configured = replace(
            original,
            actions=tuple(sorted((*original.actions, LifecycleActionKind.EXECUTE))),
            execute_profile=canonical_identity("startup"),
            execute_toolchains=(tool,),
        )
        pool._facts[worker.worker_id] = configured
        self.assertTrue(pool.supports_execute(worker, (tool,)))
        self.assertFalse(pool.supports_execute(worker, (canonical_identity("other"),)))
        with patch(
            "literate_ai.adapters.action_admission.probe_command_action_capabilities",
            return_value=replace(
                configured, execute_profile=canonical_identity("changed")
            ),
        ):
            with self.assertRaises(ActionWireError) as raised:
                pool.revalidate(worker)
            self.assertEqual(raised.exception.code, "action_admission.runtime_changed")


if __name__ == "__main__":
    unittest.main()
