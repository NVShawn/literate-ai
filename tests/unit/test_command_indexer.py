"""Production index-port publication and real command-worker handoff."""

from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.adapters.standard_project import (
    assemble_filesystem_standard_project_runtime,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.storage import FileSystemCAS
from tests.unit import test_action_source_index as source_fixture
from tests.unit.test_action_blob_source import blob_path, source_cas_server
from tests.unit.test_standard_project_factory import (
    _command_contracts,
    _toolchain_closure,
)


class CommandIndexerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.SourceIndexActionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source_root = self.fixture.root / "generated"
        self.source_root.mkdir()
        self.controller_cas = FileSystemCAS(self.fixture.root / "published")
        self.blobs = {}
        for item in self.fixture.files:
            path = self.source_root / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.fixture.cas.get_bytes(item.blob))
            self.fixture.cas.path_for(item.blob).unlink()
            self.blobs[blob_path(item.blob)] = self.controller_cas.path_for(item.blob)
        self.registry = LocalSourceTreeRegistry()
        self.registry.register(self.fixture.candidate, self.source_root)
        self.admissions = []

    def indexer(self, url):
        self.fixture.bind_remote_source(url)
        return CommandGenerationIndexer(
            self.fixture.execution_plan,
            self.registry,
            lambda _source: self.fixture.candidate,
            self.controller_cas,
            self.fixture.catalog,
            (self.fixture.request.worker,),
            self.fixture.deadline,
            cwd=self.fixture.root,
            revalidate_worker=self.admissions.append,
            environment={
                **os.environ,
                "INDEX_WORKER_IDENTITY": self.fixture.worker.identity.uri,
            },
        )

    def index(self, indexer):
        return indexer.index(
            self.fixture.candidate.component_revision,
            self.fixture.candidate.tree_identity,
        )

    def test_port_publishes_current_source_runs_worker_and_records_exact_evidence(self):
        recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=1)
        with source_cas_server(self.blobs) as (url, requests):
            indexer = self.indexer(url)
            indexer.retain_evidence_with(recorder)
            contracts, tools = _command_contracts(self.fixture.execution_plan)
            runtime = assemble_filesystem_standard_project_runtime(
                generator=lambda _prepared: None,
                object_root=self.fixture.root / "objects",
                toolchain_closure=_toolchain_closure(
                    self.fixture.execution_plan, contracts, tools
                ),
                source_trees=self.registry,
                indexer=indexer,
            )
            result = runtime.application.lifecycle.indexer.index(
                self.fixture.candidate.component_revision,
                self.fixture.candidate.tree_identity,
            )
            expected = self.fixture.expected_result()
            self.assertEqual(result, canonical_identity(expected))
            self.assertEqual(
                recorder.entries, ((result, canonical_json_bytes(expected)),)
            )
            self.assertEqual(set(path for path, _ in requests), set(self.blobs))
            self.assertEqual(len(self.admissions), 3)
            for item in self.fixture.files:
                self.assertEqual(
                    self.fixture.cas.get_bytes(item.blob),
                    (self.source_root / item.path).read_bytes(),
                )
            with self.assertRaises(ValueError):
                indexer.retain_evidence_with(recorder)
        self.assertEqual(list(self.fixture.workspace.iterdir()), [])

    def test_nonblocking_reservations_share_capacity_and_release_after_failure(self):
        with source_cas_server(self.blobs) as (url, _requests):
            indexer = self.indexer(url)
            component = self.fixture.candidate.component_revision
            source = self.fixture.candidate.tree_identity
            reservation = indexer.try_reserve_index(component, source)
            self.assertIsNotNone(reservation)
            self.assertIsNone(indexer.try_reserve_index(component, source))
            self.assertEqual(
                reservation.run(), canonical_identity(self.fixture.expected_result())
            )
            reservation.release()
            with self.assertRaises(ValueError):
                reservation.run()
            failed = indexer.try_reserve_index(
                component, canonical_identity("absent source")
            )
            with self.assertRaises(ActionWireError):
                failed.run()
            recovered = indexer.try_reserve_index(component, source)
            self.assertIsNotNone(recovered)
            recovered.release()
            recovered.release()
            with self.assertRaises(ValueError):
                recovered.run()
            final = indexer.try_reserve_index(component, source)
            self.assertIsNotNone(final)
            final.release()

    def test_changed_source_and_wrong_candidate_publish_no_blobs(self):
        with source_cas_server(self.blobs) as (url, requests):
            indexer = self.indexer(url)
            changed = replace(
                self.fixture.candidate,
                generation_key_identity=canonical_identity("other"),
            )
            indexer.source_candidate = lambda _source: changed
            with self.assertRaises(ActionWireError):
                self.index(indexer)
            indexer.source_candidate = lambda _source: self.fixture.candidate
            (self.source_root / self.fixture.files[0].path).write_bytes(b"changed")
            with self.assertRaises(ActionWireError):
                self.index(indexer)
            self.assertEqual(requests, [])
        self.assertTrue(all(not path.exists() for path in self.blobs.values()))

    def test_source_byte_and_inventory_bounds_apply_before_publication(self):
        with source_cas_server(self.blobs) as (url, requests):
            indexer = self.indexer(url)
            for bound in ("MAX_SOURCE_BYTES", "MAX_SOURCE_FILES"):
                with (
                    self.subTest(bound=bound),
                    patch(f"literate_ai.adapters.command_indexer.{bound}", 1),
                ):
                    with self.assertRaises(RuntimeError):
                        self.index(indexer)
            self.assertEqual(requests, [])
        self.assertTrue(all(not path.exists() for path in self.blobs.values()))

    def test_wrong_semantic_result_cannot_enter_qualification_recorder(self):
        recorder = QualificationEvidenceRecorder(max_bytes=4096, max_records=1)
        with source_cas_server(self.blobs) as (url, _requests):
            indexer = self.indexer(url)
            indexer.retain_evidence_with(recorder)
            original = indexer._dispatcher

            def tampered(records, results):
                delegate = original(records, results)
                dispatch = delegate.dispatch

                def altered(request):
                    outcome = dispatch(request)
                    other = canonical_json_bytes({"unrelated": "index"})
                    identity = canonical_identity({"unrelated": "index"})
                    results[identity] = other
                    return replace(outcome, result_identity=identity)

                delegate.dispatch = altered
                return delegate

            indexer._dispatcher = tampered
            with self.assertRaises(ActionWireError) as raised:
                self.index(indexer)
            self.assertEqual(raised.exception.code, "action_source.result_mismatch")
            self.assertEqual(recorder.entries, ())

    def test_concurrent_index_calls_share_the_admitted_worker_slot(self):
        entered = threading.Event()
        release = threading.Event()
        second_candidate = threading.Event()
        second_dispatch = threading.Event()
        calls = []
        candidates = []
        with source_cas_server(self.blobs) as (url, _requests):
            indexer = self.indexer(url)
            original = indexer._dispatcher

            def candidate(_source):
                candidates.append(True)
                if len(candidates) == 2:
                    second_candidate.set()
                return self.fixture.candidate

            def gated(records, results):
                delegate = original(records, results)
                dispatch = delegate.dispatch

                def wait_for_slot(request):
                    calls.append(request)
                    if len(calls) == 1:
                        entered.set()
                        if not release.wait(10):
                            raise RuntimeError(
                                "test dispatch release was not signalled"
                            )
                    else:
                        second_dispatch.set()
                    return dispatch(request)

                delegate.dispatch = wait_for_slot
                return delegate

            indexer.source_candidate = candidate
            indexer._dispatcher = gated
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.index, indexer)
                try:
                    self.assertTrue(entered.wait(10))
                    second = pool.submit(self.index, indexer)
                    self.assertTrue(second_candidate.wait(10))
                    self.assertFalse(second_dispatch.wait(0.1))
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=10), second.result(timeout=10))
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(request.slot == 0 for request in calls))
