"""Real worker indexing from verified source CAS, with refusal and cleanup."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.action_command_dispatch import (
    CommandLifecycleActionDispatcher,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_source_index import (
    execute_source_index_action,
    source_generation_result,
)
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.contracts.executable_components import GeneratedSourceCandidate
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
)
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.source_index import generated_source_tree_identity
from literate_ai.storage import FileSystemCAS
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server
from tests.support.fixtures_test_component_execution_planning import (
    _diamond_lock,
    _models,
)


class SourceIndexActionTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.cas = FileSystemCAS(self.root / "cas")
        self.workspace = self.root / "work"
        self.workspace.mkdir()
        self.worker = ExecutionWorker(
            "index",
            ExecutionWorkerKind.COMMAND,
            command=(
                sys.executable,
                "-I",
                "-m",
                "literate_ai.action_worker",
                "--cas",
                str(self.cas.root),
                "--workspace",
                str(self.workspace),
            ),
            environment=(
                ExecutionWorkerEnvironment(
                    "LITAI_ACTION_WORKER_IDENTITY", "INDEX_WORKER_IDENTITY", True
                ),
            ),
        )
        self.catalog = ExecutionWorkerCatalog((self.worker,))
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        self.results = {}
        self.fixture({"source/main.py": b"print('hello')\n", "README.md": b"source\n"})

    def fixture(self, contents):
        values = {
            item.name: canonical_identity(item.name)
            for item in fields(GeneratedSourceCandidate)
        }
        values["tree_identity"] = ContentIdentity.parse_uri(
            generated_source_tree_identity(contents)
        )
        lock = _diamond_lock()
        execution = plan_component_execution(lock, model_identities=_models(lock))
        self.execution_plan = execution
        revision = execution.root_revision
        generation_plan = next(
            item
            for item in execution.generation_plans
            if item.component_revision == revision
        )
        values["component_revision"] = revision
        values["component_generation_plan_identity"] = generation_plan.identity
        values["generation_key_identity"] = generation_plan.generation_key.identity
        self.candidate = GeneratedSourceCandidate(**values)
        self.files = tuple(
            CachedSourceFile(path, self.cas.put_bytes(content))
            for path, content in sorted(contents.items())
        )
        predecessor = source_generation_result(
            execution.identity, self.candidate, self.files
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                execution.identity,
                revision,
                LifecycleActionKind.INDEX,
                generation_plan.identity,
            )
        )
        self.records = {
            record_identity(payload): payload,
            record_identity(predecessor): predecessor,
        }
        action = next(
            item
            for item in plan_lifecycle_action_dag(
                execution, worker_ids=(self.worker.worker_id,)
            )
            if item.component_revision == revision
            and item.kind is LifecycleActionKind.INDEX
        )
        self.assertEqual(action.payload_identity, record_identity(payload))
        self.request = LifecycleActionDispatchRequest(
            canonical_identity("schedule"),
            action,
            LifecycleActionWorker(
                self.worker.worker_id,
                self.worker.identity,
                self.catalog.identity,
                canonical_identity("observation"),
            ),
            0,
            (record_identity(predecessor),),
            self.deadline.identity,
        )

    def execute(self, **kwargs):
        return execute_source_index_action(
            kwargs.pop("request", self.request),
            kwargs.pop("deadline", self.deadline),
            kwargs.pop("records", self.records),
            expected_worker_identity=kwargs.pop(
                "expected_worker_identity", self.worker.identity
            ),
            cas=self.cas,
            workspace_root=self.workspace,
            **kwargs,
        )

    def dispatch(self, *, worker_identity=None):
        dispatcher = CommandLifecycleActionDispatcher(
            self.catalog,
            (self.request.worker,),
            self.deadline,
            cwd=self.root,
            input_records=lambda request: self.records,
            record_result=lambda identity, content: self.results.update(
                {identity: content}
            ),
            revalidate_worker=lambda worker: None,
            environment={
                **os.environ,
                "INDEX_WORKER_IDENTITY": (worker_identity or self.worker.identity).uri,
            },
        )
        return dispatcher.dispatch(self.request)

    def expected_result(self):
        return {
            "schema": "literate-ai/disabled-source-index@1",
            "component_revision": self.candidate.component_revision.uri,
            "source": self.candidate.tree_identity.uri,
        }

    def change_predecessor(self, change):
        previous_identity = self.request.predecessor_result_identities[0]
        previous = json.loads(self.records[previous_identity])
        change(previous)
        content = canonical_json_bytes(previous)
        self.records.pop(previous_identity)
        self.records[record_identity(content)] = content
        self.request = replace(
            self.request, predecessor_result_identities=(record_identity(content),)
        )

    def bind_remote_source(self, url):
        self.worker = replace(
            self.worker,
            command=(*self.worker.command, "--source-cas-url", url, "--allow-http"),
        )
        self.catalog = ExecutionWorkerCatalog((self.worker,))
        self.request = replace(
            self.request,
            worker=replace(
                self.request.worker,
                worker_identity=self.worker.identity,
                catalog_identity=self.catalog.identity,
            ),
        )

    def remote_source_blobs(self):
        blobs = {}
        controller_cas = FileSystemCAS(self.root / "controller-cas")
        for item in self.files:
            reference = controller_cas.put_bytes(self.cas.get_bytes(item.blob))
            blobs[blob_path(item.blob)] = controller_cas.path_for(reference)
            self.cas.path_for(item.blob).unlink()
        return blobs

    def test_real_worker_fetches_exact_source_from_separate_controller_cas(self):
        blobs = self.remote_source_blobs()
        original = {
            path: (file.read_bytes(), file.stat().st_mtime_ns)
            for path, file in blobs.items()
        }
        with source_cas_server(blobs) as (url, requests):
            self.bind_remote_source(url)
            outcome = self.dispatch()
            self.assertIsNone(outcome.failure_code)
            self.assertEqual(
                outcome.result_identity, canonical_identity(self.expected_result())
            )
            self.assertEqual(set(path for path, _ in requests), set(blobs))
            for item in self.files:
                self.assertEqual(
                    self.cas.get_bytes(item.blob),
                    blobs[blob_path(item.blob)].read_bytes(),
                )
            requests.clear()
            self.assertIsNone(self.dispatch().failure_code)
            self.assertEqual(requests, [])
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(
            original,
            {
                path: (file.read_bytes(), file.stat().st_mtime_ns)
                for path, file in blobs.items()
            },
        )

    def test_remote_corruption_never_publishes_a_source_blob_or_result(self):
        blobs = self.remote_source_blobs()
        with source_cas_server(blobs, mode="corrupt") as (url, _requests):
            self.bind_remote_source(url)
            self.assertEqual(
                self.dispatch().failure_code, "action_source.digest_mismatch"
            )
        self.assertEqual(self.results, {})
        self.assertTrue(all(not self.cas.contains(item.blob) for item in self.files))
        self.assertEqual(list(self.workspace.iterdir()), [])
