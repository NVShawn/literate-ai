"""Controller LINK uses the real receiver and shares admitted worker capacity."""

import os
import sys
import unittest
from types import SimpleNamespace

import tests.support.fixtures_test_action_link as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.command_linker import CommandComponentLinker
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)


class CommandLinkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.LinkActionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.plan = f.value.build.plan
        self.receipt = f.value.acceptance_result.evidence
        jobs = f.source.root.parent / "link-jobs"
        jobs.mkdir()
        self.worker = ExecutionWorker(
            "link",
            ExecutionWorkerKind.COMMAND,
            environment=(
                ExecutionWorkerEnvironment(
                    "LITAI_ACTION_WORKER_IDENTITY", "LITAI_ACTION_WORKER_IDENTITY", True
                ),
            ),
            command=(
                sys.executable,
                "-I",
                "-m",
                "literate_ai.action_worker",
                "--cas",
                str(f.source.root),
                "--workspace",
                str(jobs),
            ),
        )
        catalog = ExecutionWorkerCatalog((self.worker,))
        self.admitted = LifecycleActionWorker(
            "link",
            self.worker.identity,
            catalog.identity,
            canonical_identity("hardware"),
        )
        self.available, self.healthy = True, True

        def guard(worker):
            self.assertEqual(worker, self.admitted)
            if not self.healthy:
                raise ActionWireError("fixture.unhealthy", "worker unavailable")

        ports = f.fixture.fixture.ports
        self.indexer = CommandGenerationIndexer(
            f.value.build.execution_plan,
            ports.source_trees,
            lambda source: ports.source_trees.evidence(source).candidate,
            f.cas,
            catalog,
            (self.admitted,),
            f.fixture.deadline,
            cwd=jobs.parent,
            revalidate_worker=guard,
            environment=dict(
                os.environ, LITAI_ACTION_WORKER_IDENTITY=self.worker.identity.uri
            ),
        )
        self.admission = SimpleNamespace(
            catalog=catalog,
            workers=(self.admitted,),
            identity=canonical_identity("admission"),
            supports_phase=lambda worker, phase: (
                self.available
                and worker == self.admitted
                and phase is LifecycleActionKind.LINK
            ),
        )
        self.linker = CommandComponentLinker(
            self.indexer,
            self.admission,
            handoff_for=lambda plan, receipt: (
                f.value.acceptance_input,
                f.value.acceptance_result,
            ),
            result_source=lambda worker, reference: f.source.get_bytes(reference),
        )

    def test_real_receiver_returns_controller_verified_manifest(self):
        reservation = self.linker.try_reserve_link(self.plan, self.receipt)
        self.assertIsNotNone(reservation)
        result = reservation.run()
        self.assertEqual(result, self.fixture.value.manifest)
        self.assertEqual(len(self.linker._completed), 1)
        self.assert_released()

    def assert_released(self):
        reservation = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reservation)
        reservation.release()

    def test_busy_pool_returns_none_and_early_release_returns_capacity(self):
        held = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNone(self.linker.try_reserve_link(self.plan, self.receipt))
        held.release()
        reservation = self.linker.try_reserve_link(self.plan, self.receipt)
        reservation.release()
        self.assert_released()
        self.assertEqual(self.linker._completed, {})

    def test_authority_loss_after_reservation_refuses_and_releases(self):
        reservation = self.linker.try_reserve_link(self.plan, self.receipt)
        self.healthy = False
        with self.assertRaises(ActionWireError):
            reservation.run()
        self.assert_released()
        self.assertEqual(self.linker._completed, {})

    def test_corrupt_controller_return_transfer_cannot_complete_link(self):
        self.linker.result_source = lambda worker, reference: b"corrupt"
        with self.assertRaises(ActionWireError) as error:
            self.linker.link(self.plan, self.receipt)
        self.assertEqual(error.exception.code, "action_build.provider_invalid")
        self.assert_released()
        self.assertEqual(self.linker._completed, {})

    def test_local_acceptance_handoff_reaches_real_link_receiver(self):
        from literate_ai.adapters.local_accept_handoff import LocalAcceptanceLinkHandoff

        accepted = self.fixture.fixture.fixture
        accepted.transfer()
        local = LocalAcceptanceLinkHandoff(
            accepted.ports,
            self.indexer,
            accepted.ports,
            handoff_for=lambda *args: accepted.value,
        )
        receipt = local.accept(
            self.plan,
            self.receipt.generated_tests.identity,
            self.receipt.execution.identity,
        )
        self.linker.handoff_for = local.link_handoff
        result = self.linker.link(self.plan, receipt)
        self.assertEqual(result, self.fixture.value.manifest)
        self.assert_released()

    def test_handoff_change_during_return_cannot_complete_link(self):
        original = self.linker.handoff_for
        changed = False

        def handoff(plan, receipt):
            if changed:
                raise ActionWireError("fixture.changed", "acceptance changed")
            return original(plan, receipt)

        def fetch(worker, reference):
            nonlocal changed
            changed = True
            return self.fixture.source.get_bytes(reference)

        self.linker.handoff_for = handoff
        self.linker.result_source = fetch
        with self.assertRaises(ActionWireError) as error:
            self.linker.link(self.plan, self.receipt)
        self.assertEqual(error.exception.code, "fixture.changed")
        self.assertEqual(self.linker._completed, {})
        self.assert_released()

    def test_worker_loss_during_return_aborts_live_verification(self):
        def fetch(worker, reference):
            self.healthy = False
            return self.fixture.source.get_bytes(reference)

        self.linker.result_source = fetch
        with self.assertRaises(ActionWireError) as error:
            self.linker.link(self.plan, self.receipt)
        self.assertEqual(error.exception.code, "fixture.unhealthy")
        self.assertEqual(self.linker._completed, {})
        self.assert_released()
