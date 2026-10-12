"""Real GENERATE command transport, shared capacity and source proof import."""

import os
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace

import tests.support.fixtures_test_action_generate_source as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_generator import CommandSourceGenerator
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.storage import FileSystemCAS


class CommandGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.GenerateSourceTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        value = f.fixture.value
        root = f.fixture.fixture.root
        self.marker = root / "dispatched"
        result_path = root / "result.json"
        result_path.write_bytes(f.fixture.result.to_bytes())
        code = f"""
import sys
from pathlib import Path
from literate_ai.adapters.action_dispatch_wire import (
 decode_action_request,encode_action_response,MAX_ACTION_WIRE_BYTES,
)
from literate_ai.adapters.action_generate import admit_generate_action
request,deadline,records=decode_action_request(sys.stdin.buffer.read(MAX_ACTION_WIRE_BYTES+1))
admit_generate_action(request,deadline,records,expected_worker_identity=request.worker.worker_identity)
assert not request.action.predecessor_ids
assert not request.predecessor_result_identities
Path({str(self.marker)!r}).touch()
sys.stdout.buffer.write(encode_action_response(request,result_record=Path({str(result_path)!r}).read_bytes()))
"""
        self.worker = ExecutionWorker(
            "generator",
            ExecutionWorkerKind.COMMAND,
            command=(sys.executable, "-I", "-c", code),
        )
        self.catalog = ExecutionWorkerCatalog((self.worker,))
        self.admitted = LifecycleActionWorker(
            self.worker.worker_id,
            self.worker.identity,
            self.catalog.identity,
            canonical_identity("observed"),
        )
        self.healthy, self.available = True, True

        def revalidate(worker):
            self.assertEqual(worker, self.admitted)
            if not self.healthy:
                raise ActionWireError("fixture.changed", "worker changed")

        self.indexer = CommandGenerationIndexer(
            value.execution_plan,
            f.registry,
            lambda source: f.registry.evidence(source).candidate,
            f.cas,
            self.catalog,
            (self.admitted,),
            f.fixture.deadline,
            cwd=root,
            revalidate_worker=revalidate,
            environment=dict(os.environ),
        )
        self.admission = SimpleNamespace(
            catalog=self.catalog,
            workers=(self.admitted,),
            identity=canonical_identity("admission"),
            supports_phase=lambda worker, phase: (
                self.available
                and worker == self.admitted
                and phase is LifecycleActionKind.GENERATE
            ),
        )
        self.generator = CommandSourceGenerator(
            self.indexer,
            self.admission,
            cache_key_provider=f.fixture.fixture.runner.planned_cache_key,
            result_source=self.fetch,
        )

    def fetch(self, worker, reference):
        self.assertEqual(worker, self.worker)
        return self.fixture.fixture.fixture.cas.get_bytes(reference)

    def reserve(self):
        return self.generator.try_reserve_generate(self.fixture.prepared)

    def assert_released(self):
        reservation = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reservation)
        reservation.release()

    def test_command_result_imports_source_into_controller_registry(self):
        output = self.generator(self.fixture.prepared)
        self.assertEqual(output, self.fixture.fixture.output)
        self.assertEqual(
            self.fixture.registry.resolve(output.candidate.tree_identity),
            self.fixture.root,
        )
        self.assertTrue(self.marker.exists())
        self.assert_released()

    def test_shared_capacity_is_reserved_before_dispatch(self):
        occupied = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNone(self.reserve())
        self.assertFalse(self.marker.exists())
        occupied.release()
        reservation = self.reserve()
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.run(), self.fixture.fixture.output)
        self.assert_released()

    def test_worker_loss_after_reservation_refuses_and_releases(self):
        reservation = self.reserve()
        self.healthy = False
        with self.assertRaises(ActionWireError):
            reservation.run()
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_capability_loss_refuses_before_reservation(self):
        self.available = False
        with self.assertRaises(ActionWireError):
            self.reserve()
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_bad_proof_does_not_register_source_and_releases_capacity(self):
        self.generator.result_source = lambda worker, ref: b"corrupt"
        with self.assertRaises(ActionWireError):
            self.generator(self.fixture.prepared)
        self.assertEqual(self.fixture.registry._paths, {})
        self.assertEqual(list(self.fixture.root.iterdir()), [])
        self.assert_released()

    def test_generated_candidate_keeps_exact_planned_key_without_cache_publication(
        self,
    ):
        planned = self.generator.planned_cache_key(self.fixture.prepared)
        candidate = self.fixture.fixture.output.candidate
        with self.assertRaises(ActionWireError):
            self.generator.cache_key_for_candidate(candidate)
        output = self.generator(self.fixture.prepared)
        self.assertEqual(
            self.generator.cache_key_for_candidate(output.candidate), planned
        )
        underlying = self.fixture.fixture.fixture.generator
        self.assertEqual(underlying.publications, 0)

    def test_wrong_planned_request_cannot_import_or_capture_key(self):
        key = self.generator.planned_cache_key(self.fixture.prepared)
        self.generator.cache_key_provider = lambda prepared: replace(
            key, request_identity=canonical_identity("foreign")
        )
        with self.assertRaises(ActionWireError) as error:
            self.generator(self.fixture.prepared)
        self.assertEqual(error.exception.code, "action_generate.cache_key_invalid")
        self.assertEqual(self.fixture.registry._paths, {})
        with self.assertRaises(ActionWireError):
            self.generator.cache_key_for_candidate(
                self.fixture.fixture.output.candidate
            )
        self.assert_released()

    def test_key_change_during_return_transport_refuses_source_publication(self):
        key = self.generator.planned_cache_key(self.fixture.prepared)

        def fetch(worker, reference):
            self.generator.cache_key_provider = lambda prepared: replace(
                key, source_semantics_identity=canonical_identity("changed")
            )
            return self.fetch(worker, reference)

        self.generator.result_source = fetch
        with self.assertRaises(ActionWireError) as error:
            self.generator(self.fixture.prepared)
        self.assertEqual(error.exception.code, "action_generate.cache_key_changed")
        self.assertEqual(self.fixture.registry._paths, {})
        self.assert_released()

    def test_restored_candidate_key_is_recipe_bound_and_cannot_be_replaced(self):
        candidate = self.fixture.fixture.output.candidate
        key = self.generator.planned_cache_key(self.fixture.prepared)
        self.generator.record_restored_cache_key(candidate, key)
        self.generator.record_restored_cache_key(candidate, key)
        self.assertEqual(self.generator.cache_key_for_candidate(candidate), key)
        for changed in (
            replace(key, recipe_identity=canonical_identity("foreign")),
            replace(key, request_identity=canonical_identity("other-request")),
        ):
            with self.assertRaises(ActionWireError):
                self.generator.record_restored_cache_key(candidate, changed)

    def test_separate_candidate_store_retains_worker_proof_for_standard(self):
        candidate_cas = FileSystemCAS(
            self.fixture.fixture.fixture.root / "standard-candidates"
        )
        self.generator.candidate_cas = candidate_cas
        output = self.generator(self.fixture.prepared)
        reference = next(
            ref
            for ref in self.fixture.fixture.result.evidence_records
            if ref.identity == output.candidate.source_manifest_identity.uri
        )
        self.assertEqual(
            candidate_cas.get_bytes(reference), self.fetch(self.worker, reference)
        )
        self.assertEqual(
            self.fixture.registry.resolve(output.candidate.tree_identity),
            self.fixture.root,
        )

    def test_retained_source_requires_exact_authorization_before_dispatch(self):
        from literate_ai.adapters.retained_source import RetainedSourceError

        retained, _ = self.fixture.fixture.fixture._retained_input()
        with self.assertRaises(RetainedSourceError):
            CommandSourceGenerator(
                self.indexer,
                self.admission,
                cache_key_provider=self.fixture.fixture.fixture.runner.planned_cache_key,
                retained_source=retained,
            )
        self.assertFalse(self.marker.exists())

    def test_changed_retained_source_refuses_before_capacity_or_dispatch(self):
        from literate_ai.adapters.retained_source import RetainedSourceError

        retained, _ = self.fixture.fixture.fixture._retained_input()
        generator = CommandSourceGenerator(
            self.indexer,
            self.admission,
            cache_key_provider=self.fixture.fixture.fixture.runner.planned_cache_key,
            retained_source=retained,
            retained_source_authorization=retained.identity.uri,
        )
        name = retained.files[0][0].removeprefix("source/")
        (retained.root / name).write_bytes(b"changed")
        with self.assertRaises(RetainedSourceError):
            generator.try_reserve_generate(self.fixture.prepared)
        self.assertFalse(self.marker.exists())
        self.assert_released()
