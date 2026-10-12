"""FINALIZE controller slots and exact dispatch; return verifier is isolated here."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tests.support.fixtures_test_action_finalize_result_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize import admit_finalize_action
from literate_ai.adapters.action_slots import CommandActionSlots
from literate_ai.adapters.command_finalizer import CommandProjectFinalizer
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class CommandFinalizerTests(unittest.TestCase):
    def setUp(self):
        f = fixture_module.FinalizeResultTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.value = f.value
        self.package_input = f.value.package_input
        self.package = f.fixture.package
        from tests.support.fixtures_test_action_finalize import make_finalize_request

        f.request, _ = make_finalize_request(f.value, {}, f.fixture.deadline)
        self.worker = f.request.worker
        self.proof = {
            identity: canonical_json_bytes(action)
            for action, identity in self.package_input.link_results
        }
        self.profile = canonical_identity("finalize-profile")
        self.available = True
        self.admission = SimpleNamespace(
            identity=canonical_identity("admission"),
            supports_finalize=lambda worker, identity: (
                self.available and worker == self.worker and identity == self.profile
            ),
        )
        self.indexer = SimpleNamespace(
            workers=(self.worker,),
            execution_plan=self.package_input.execution_plan,
            deadline=f.fixture.deadline,
            slots=CommandActionSlots((self.worker,), f.fixture.deadline),
            revalidate_worker=Mock(),
            require_worker_current=Mock(),
            catalog=SimpleNamespace(worker=lambda name: name),
            cas=object(),
            remember_action_result=Mock(),
        )
        self.handoff = Mock(return_value=(self.package_input, self.proof))
        self.linker = SimpleNamespace(
            indexer=self.indexer,
            admission=self.admission,
            package_handoff=self.handoff,
        )
        self.controller = CommandProjectFinalizer(
            self.linker,
            profile_identity=self.profile,
            verify_package=Mock(),
            verify_stages=Mock(),
            result_source=Mock(),
        )
        self.dispatch = Mock(side_effect=self.return_result)
        self.indexer._dispatcher = self.dispatch
        self.importer = self.enterContext(
            patch(
                "literate_ai.adapters.command_finalizer.import_finalize_result",
                return_value=object(),
            )
        )

    def return_result(self, records, returned):
        def dispatch(request):
            self.assertEqual(
                admit_finalize_action(
                    request,
                    self.indexer.deadline,
                    records,
                    expected_worker_identity=self.worker.worker_identity,
                ),
                self.value,
            )
            raw = b"fixture result: importer is tested independently"
            identity = record_identity(raw)
            returned[identity] = raw
            return SimpleNamespace(failure_code=None, result_identity=identity)

        return SimpleNamespace(dispatch=dispatch)

    def reserve(self):
        return self.controller.try_reserve_finalize(
            self.value.component_lock,
            self.value.project_plan,
            self.package_input.artifact_graph,
            self.package_input.plan,
            self.package,
        )

    def assert_released(self):
        held = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(held)
        held.release()

    def test_exact_dispatch_imports_and_releases_shared_capacity(self):
        result = self.reserve().run()
        self.assertIs(result, self.importer.return_value)
        self.importer.assert_called_once()
        self.assertEqual(
            set(self.importer.call_args.kwargs["records"]),
            set(self.proof) | {self.value.package_result_identity},
        )
        self.assertIs(
            self.importer.call_args.kwargs["verify_package"],
            self.controller.verify_package,
        )
        self.assert_released()

    def test_busy_pool_does_not_dispatch(self):
        held = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNone(self.reserve())
        self.dispatch.assert_not_called()
        held.release()
        self.assert_released()

    def test_profile_loss_after_reservation_refuses_and_releases(self):
        reservation = self.reserve()
        self.available = False
        with self.assertRaises(ActionWireError) as error:
            reservation.run()
        self.assertEqual(error.exception.code, "action_finalize.authority_changed")
        self.dispatch.assert_not_called()
        self.assert_released()

    def test_no_exact_finalizer_refuses_before_dispatch(self):
        self.available = False
        with self.assertRaises(ActionWireError) as error:
            self.reserve()
        self.assertEqual(error.exception.code, "action_finalize.unavailable")
        self.dispatch.assert_not_called()
        self.assert_released()

    def test_handoff_change_during_import_refuses_result(self):
        def changed(**kwargs):
            self.handoff.return_value = (self.package_input, {})
            return object()

        self.importer.side_effect = changed
        with self.assertRaises(ActionWireError) as error:
            self.reserve().run()
        self.assertEqual(error.exception.code, "action_finalize.authority_changed")
        self.indexer.remember_action_result.assert_not_called()
        self.assert_released()

    def test_import_failure_releases_capacity_without_retaining_result(self):
        self.importer.side_effect = ActionWireError("fixture.corrupt", "bad return")
        with self.assertRaises(ActionWireError):
            self.reserve().run()
        self.indexer.remember_action_result.assert_not_called()
        self.assert_released()

    def test_worker_loss_during_import_refuses_result(self):
        def changed(**kwargs):
            self.available = False
            kwargs["admission_guard"]()

        self.importer.side_effect = changed
        with self.assertRaises(ActionWireError) as error:
            self.reserve().run()
        self.assertEqual(error.exception.code, "action_finalize.authority_changed")
        self.indexer.remember_action_result.assert_not_called()
        self.assert_released()

    def test_worker_failure_is_not_replaced_by_local_packaging(self):
        self.dispatch.side_effect = None
        self.dispatch.return_value = SimpleNamespace(
            dispatch=lambda request: SimpleNamespace(failure_code="fixture.failed")
        )
        with self.assertRaises(ActionWireError) as error:
            self.reserve().run()
        self.assertEqual(error.exception.code, "fixture.failed")
        self.importer.assert_not_called()
        self.controller.verify_package.assert_not_called()
        self.indexer.remember_action_result.assert_not_called()
        self.assert_released()
