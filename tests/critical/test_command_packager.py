"""Controller PACKAGE capacity, wire binding and admission-change regression checks."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tests.support.fixtures_test_action_package as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_package import admit_package_action
from literate_ai.adapters.action_slots import CommandActionSlots
from literate_ai.adapters.command_packager import CommandProjectPackager
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class CommandPackagerTests(unittest.TestCase):
    def setUp(self):
        f = fixture_module.PackageActionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.value = f.fixture.value
        self.worker = f.request.worker
        self.proof = {
            identity: canonical_json_bytes(action)
            for action, identity in self.value.link_results
        }
        self.available = True
        self.admission = SimpleNamespace(
            identity=canonical_identity("admission"),
            supports_package=lambda worker, identity: (
                self.available
                and worker == self.worker
                and identity == self.value.plan.packager_identity
            ),
        )
        self.indexer = SimpleNamespace(
            workers=(self.worker,),
            execution_plan=self.value.execution_plan,
            deadline=f.fixture.deadline,
            slots=CommandActionSlots((self.worker,), f.fixture.deadline),
            revalidate_worker=Mock(),
            require_worker_current=Mock(),
            catalog=SimpleNamespace(worker=lambda name: name),
            cas=object(),
            remember_action_result=Mock(),
        )
        self.handoff = Mock(return_value=(self.value, self.proof))
        self.linker = SimpleNamespace(
            indexer=self.indexer,
            admission=self.admission,
            package_handoff=self.handoff,
        )
        self.controller = CommandProjectPackager(
            self.linker, verify_package=Mock(), result_source=Mock()
        )
        self.dispatch = Mock(side_effect=self.return_result)
        self.indexer._dispatcher = self.dispatch
        self.importer = self.enterContext(
            patch(
                "literate_ai.adapters.command_packager.import_package_result",
                return_value=object(),
            )
        )

    def return_result(self, records, returned):
        def dispatch(request):
            self.assertEqual(
                admit_package_action(
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
        return self.controller.try_reserve_package(
            self.value.artifact_graph, self.value.plan
        )

    def assert_released(self):
        held = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(held)
        held.release()

    def test_exact_dispatch_imports_and_releases_shared_capacity(self):
        result = self.reserve().run()
        self.assertIs(result, self.importer.return_value)
        self.importer.assert_called_once()
        self.assertEqual(self.importer.call_args.kwargs["records"], self.proof)
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
        self.assertEqual(error.exception.code, "action_package.authority_changed")
        self.dispatch.assert_not_called()
        self.assert_released()

    def test_no_exact_packager_refuses_before_dispatch(self):
        self.available = False
        with self.assertRaises(ActionWireError) as error:
            self.reserve()
        self.assertEqual(error.exception.code, "action_package.unavailable")
        self.dispatch.assert_not_called()
        self.assert_released()

    def test_handoff_change_during_import_refuses_result(self):
        def changed(**kwargs):
            self.handoff.return_value = (self.value, {})
            return object()

        self.importer.side_effect = changed
        with self.assertRaises(ActionWireError) as error:
            self.reserve().run()
        self.assertEqual(error.exception.code, "action_package.authority_changed")
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
        self.assertEqual(error.exception.code, "action_package.authority_changed")
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

    def test_local_input_bridge_publishes_exact_bytes_and_rechecks_after_dispatch(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from literate_ai.storage.cas import FileSystemCAS

        with TemporaryDirectory() as directory:
            self.indexer.cas = FileSystemCAS(Path(directory))
            ref = self.indexer.cas.put_bytes(b"package input")
            plan = SimpleNamespace(
                inputs=(SimpleNamespace(blob=ref),),
                packager_identity=self.value.plan.packager_identity,
            )
            reader = Mock(return_value=b"package input")
            with patch.object(self.controller, "package", return_value=object()) as run:
                result = self.controller.package_local_inputs(
                    self.value.artifact_graph, plan, read_blob=reader
                )
                self.assertIs(result, run.return_value)
                self.assertEqual(reader.call_count, 2)
                reader.side_effect = [b"package input", b"changed"]
                with self.assertRaises(ActionWireError) as error:
                    self.controller.package_local_inputs(
                        self.value.artifact_graph, plan, read_blob=reader
                    )
                self.assertEqual(error.exception.code, "action_package.input_changed")
                run.reset_mock()
                reader.side_effect = None
                reader.return_value = b"corrupt"
                with self.assertRaises(ActionWireError):
                    self.controller.package_local_inputs(
                        self.value.artifact_graph, plan, read_blob=reader
                    )
                run.assert_not_called()

    def test_oversized_local_inputs_refuse_before_read_or_dispatch(self):
        from literate_ai.contracts import BlobRef

        plan = SimpleNamespace(
            inputs=(SimpleNamespace(blob=BlobRef("a" * 64, 257 * 1024 * 1024)),),
            packager_identity=self.value.plan.packager_identity,
        )
        reader = Mock()
        with patch.object(self.controller, "package") as run:
            with self.assertRaises(ActionWireError) as error:
                self.controller.package_local_inputs(
                    self.value.artifact_graph, plan, read_blob=reader
                )
            self.assertEqual(error.exception.code, "action_package.bytes_exceeded")
            reader.assert_not_called()
            run.assert_not_called()
