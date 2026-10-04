"""Shared test fixtures extracted from test_action_build_result."""

import json
import os
import shutil
import stat
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_build_result import (
    MAX_BUILD_ARCHIVE_BYTES,
    BuildWorkerResult,
    capture_build_result,
    import_build_result,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.directory_artifacts import (
    encode_directory_export,
    read_directory_export,
)
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.security import AuthorizationError
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import BlobIntegrityError
from tests.support import fixtures_test_standard_transferred_build as transfer_fixture
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class ActionBuildResultTests(unittest.TestCase):
    def setUp(self):
        fixture = transfer_fixture.StandardTransferredBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.admit()
        self.fixture = fixture
        self.root = fixture.root
        self.worker = fixture.receiver
        self.worker_cas = FileSystemCAS(self.root / "worker-cas")
        self.cas = FileSystemCAS(self.root / "controller-cas")
        custody = self.worker.source_trees.evidence(
            fixture.plan.request.source_tree_identity
        )
        source = self.worker.source_trees.resolve(custody.candidate.tree_identity)
        files = tuple(
            CachedSourceFile(
                path.relative_to(source).as_posix(), self.worker_cas.put_file(path)
            )
            for path in sorted(source.rglob("*"))
            if path.is_file()
        )
        _, execution = _fixture()
        self.input = BuildWorkerInput(
            execution.identity,
            custody.candidate.component_generation_plan_identity,
            custody.candidate,
            fixture.plan,
            fixture.inputs,
            files,
            self.worker.source_trees.validation_inputs(custody.candidate.tree_identity),
            custody.source_generation_identity,
            custody.identity,
            execution.generation_plans[0],
            execution,
        )
        self.input_record = self.input.to_bytes()
        self.input_identity = record_identity(self.input_record)
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        self.controller = LocalStandardLifecyclePorts(
            source_trees=self.worker.source_trees,
            object_root=self.root / "controller",
            contracts=tuple(self.worker.contracts.values()),
            tool_bindings=tuple(self.worker.tool_bindings.values()),
        )
        intent = self.controller.create(
            execution, execution.generation_plans[0], custody.candidate, (), ()
        )
        self.controller.accept_finalized_plan(
            intent, fixture.inputs.authorization, fixture.plan
        )
        self.retained = {}
        self.content = self.capture()
        self.result = BuildWorkerResult.admit(
            self.content,
            record_identity(self.content),
            input_identity=self.input_identity,
            input_record=self.input_record,
            deadline=self.deadline,
        )

    def capture(self):
        return capture_build_result(
            input_record=self.input_record,
            input_identity=self.input_identity,
            deadline=self.deadline,
            ports=self.worker,
            output=self.fixture.output,
            records=self.fixture.recorder.entries,
            cas=self.worker_cas,
        )

    def receive(self, **changes):
        arguments = dict(
            content=self.content,
            result_identity=record_identity(self.content),
            input_record=self.input_record,
            input_identity=self.input_identity,
            deadline=self.deadline,
            ports=self.controller,
            cas=self.cas,
            retain_record=self.retained.__setitem__,
            blob_source=self.worker_cas.get_bytes,
        )
        arguments.update(changes)
        return import_build_result(**arguments)

    def assert_unregistered(self):
        self.assertEqual(self.controller._artifact_paths, {})
        self.assertEqual(list(self.controller.object_root.iterdir()), [])

    def test_http_result_transfer_survives_worker_artifact_deletion(self):
        references = (*self.result.evidence_records, self.result.artifact_archive)
        blobs = {blob_path(item): self.worker_cas.path_for(item) for item in references}
        shutil.rmtree(self.worker.object_root)
        with source_cas_server(blobs) as (endpoint, requests):
            source = HttpActionBlobSource(endpoint, self.deadline, allow_http=True)
            output = self.receive(blob_source=source.fetch)
        self.assertEqual(output, self.fixture.output)
        self.assertEqual(self.retained[record_identity(self.content)], self.content)
        for identity, payload in self.fixture.recorder.entries:
            self.assertEqual(self.retained[identity], payload)
        self.assertEqual(len(requests), len({item.identity for item in references}))
        tests = self.controller.test(self.fixture.plan, output.exports)
        self.assertEqual(tests.component_revision, self.fixture.plan.component_revision)
        self.assertEqual(
            self.controller.artifact_path(output.exports[0]).read_text(),
            "known-output\n",
        )

    def test_substituted_or_unbounded_result_refuses_before_fetch(self):
        fetch = Mock(side_effect=AssertionError("must not fetch"))
        for result in (
            replace(self.result, input_identity=canonical_identity("other")),
            replace(
                self.result,
                artifact_archive=replace(
                    self.result.artifact_archive, size=MAX_BUILD_ARCHIVE_BYTES + 1
                ),
            ),
            replace(
                self.result,
                evidence_records=tuple(reversed(self.result.evidence_records)),
            ),
        ):
            content = result.to_bytes()
            with self.assertRaises(ActionWireError):
                self.receive(
                    content=content,
                    result_identity=record_identity(content),
                    blob_source=fetch,
                )
        fetch.assert_not_called()
        self.assert_unregistered()

    def test_corrupt_fetch_never_admits_or_leaves_stage(self):
        with self.assertRaises(ActionWireError):
            self.receive(blob_source=lambda ref: b"corrupt")
        self.assert_unregistered()
        self.assertEqual(self.retained, {})

    def test_valid_hash_for_changed_archive_cannot_replace_build_bytes(self):
        files = read_directory_export(
            self.worker_cas.get_bytes(self.result.artifact_archive),
            self.result.artifact_archive,
            max_bytes=MAX_BUILD_ARCHIVE_BYTES,
            max_entries=65534,
        )
        changed = tuple(
            replace(item, content=b"other") if item.path == "app" else item
            for item in files
        )
        archive = encode_directory_export(
            changed, max_bytes=MAX_BUILD_ARCHIVE_BYTES, max_entries=65534
        )
        result = replace(
            self.result, artifact_archive=self.worker_cas.put_bytes(archive)
        )
        content = result.to_bytes()
        with self.assertRaises(ActionWireError):
            self.receive(content=content, result_identity=record_identity(content))
        self.assert_unregistered()
        self.assertEqual(self.retained, {})

    def test_retention_failure_or_expired_authority_cleans_stage(self):
        def fail(identity, content):
            raise RuntimeError("retention failed")

        with self.assertRaisesRegex(RuntimeError, "retention failed"):
            self.receive(retain_record=fail)
        self.assert_unregistered()

        def expire(identity, content):
            self.controller.clock = lambda: (
                self.input.inputs.authorization.grant.expires_at
            )

        with self.assertRaises(AuthorizationError):
            self.receive(retain_record=expire)
        self.assert_unregistered()

    def test_changed_worker_tree_during_capture_never_returns_result(self):
        original = self.worker_cas.put_bytes

        def mutate(content, **kwargs):
            result = original(content, **kwargs)
            (self.fixture.artifact / "app").write_bytes(b"changed")
            return result

        with patch.object(self.worker_cas, "put_bytes", side_effect=mutate):
            with self.assertRaises(ValueError):
                self.capture()

    def test_closed_control_record_and_wrong_digest_are_refused(self):
        document = json.loads(self.content)
        for content in (
            b'{"schema":1,"schema":2}',
            canonical_json_bytes({**document, "path": "/outside"}),
        ):
            with self.assertRaises(ActionWireError):
                self.receive(content=content, result_identity=record_identity(content))
        with self.assertRaises(ActionWireError):
            self.receive(result_identity=canonical_identity("wrong"))
        self.assert_unregistered()

    def test_corrupt_cached_blob_is_not_repaired_from_worker(self):
        reference = self.result.evidence_records[0]
        self.cas.put_bytes(self.worker_cas.get_bytes(reference))
        self.cas.path_for(reference).write_bytes(b"corrupt cached bytes")
        fetch = Mock(side_effect=AssertionError("must not repair"))
        with self.assertRaises(BlobIntegrityError):
            self.receive(blob_source=fetch)
        fetch.assert_not_called()
        self.assert_unregistered()

    def test_archive_preserves_readonly_mode_and_failed_stage_is_removed(self):
        export = self.fixture.artifact / "app"
        export.chmod(0o444)
        try:
            content = self.capture()

            def fail(identity, payload):
                raise RuntimeError("retention failed")

            with self.assertRaisesRegex(RuntimeError, "retention failed"):
                self.receive(
                    content=content,
                    result_identity=record_identity(content),
                    retain_record=fail,
                )
            self.assert_unregistered()
            output = self.receive(
                content=content, result_identity=record_identity(content)
            )
            path = self.controller.artifact_path(output.exports[0])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
            path.chmod(0o644)
        finally:
            export.chmod(0o644)

    def test_replaced_stage_is_preserved_on_failed_import(self):
        replacement = []

        def replace_stage(identity, payload):
            if replacement:
                return
            stage = next(self.controller.object_root.glob("build-*"))
            stage.rename(self.controller.object_root / "moved-owned-stage")
            stage.mkdir()
            (stage / "foreign").write_bytes(b"preserve")
            replacement.append(stage)

        with self.assertRaises(ValueError):
            self.receive(retain_record=replace_stage)
        self.assertEqual(self.controller._artifact_paths, {})
        self.assertEqual((replacement[0] / "foreign").read_bytes(), b"preserve")

    def test_mode_change_during_local_evidence_checks_prevents_registration(self):
        original = self.controller._record_evidence
        changed = False

        def record(document):
            nonlocal changed
            identity = original(document)
            if not changed:
                stage = next(self.controller.object_root.glob("build-*"))
                (stage / "app").chmod(0o444)
                changed = True
            return identity

        with patch.object(self.controller, "_record_evidence", side_effect=record):
            with self.assertRaises(ValueError):
                self.receive()
        self.assertTrue(changed)
        self.assert_unregistered()
