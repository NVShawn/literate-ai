"""Shared fixtures extracted from ``tests.unit.test_action_build_source``."""

import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_build_source import materialize_build_source
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.adapters.source_evidence_validation import (
    SourceEvidenceValidationInputs,
)
from literate_ai.contracts import ComponentCommandPhase, canonical_identity
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.security import AuthorizationError
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_action_blob_source import blob_path, source_cas_server
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_standard_local_command_adapter import (
    _python_copy_lifecycle,
)
from tests.unit.action_deadline import ACTION_TEST_DEADLINE


class ActionBuildSourceTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        controller = self.root / "controller"
        controller.mkdir()
        self.ports, _, self.candidate, intent = _python_copy_lifecycle(controller)
        authorization = self.ports.authorize(
            intent,
            self.ports.index(
                self.candidate.component_revision, self.candidate.tree_identity
            ),
        )
        self.inputs = self.ports.plan_finalization_inputs(intent, authorization)
        self.plan = self.inputs.finalize()
        self.custody = self.ports.source_trees.evidence(self.candidate.tree_identity)
        self.validation = SourceEvidenceValidationInputs.from_dict(
            self.ports.source_trees.validation_inputs(
                self.candidate.tree_identity
            ).to_dict()
        )
        source = self.ports.source_trees.resolve(self.candidate.tree_identity)
        self.controller_cas = FileSystemCAS(self.root / "controller-cas")
        self.files = tuple(
            CachedSourceFile(
                path.relative_to(source).as_posix(), self.controller_cas.put_file(path)
            )
            for path in sorted(source.rglob("*"))
            if path.is_file()
        )
        self.cas = FileSystemCAS(self.root / "worker-cas")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        self.now = authorization.grant.issued_at + timedelta(seconds=1)

    def scope(self, **changes):
        arguments = dict(
            plan=self.plan,
            inputs=self.inputs,
            candidate=self.candidate,
            files=self.files,
            validation_inputs=self.validation,
            source_generation_identity=self.custody.source_generation_identity,
            source_custody_identity=self.custody.identity,
            deadline=self.deadline,
            cas=self.cas,
            workspace_root=self.workspace,
            blob_source=lambda ref: self.controller_cas.get_bytes(ref),
            clock=lambda: self.now,
        )
        arguments.update(changes)
        return materialize_build_source(**arguments)

    def test_http_source_transfer_builds_after_controller_source_removal(self):
        blobs = {
            blob_path(item.blob): self.controller_cas.path_for(item.blob)
            for item in self.files
        }
        shutil.rmtree(self.root / "controller")
        with source_cas_server(blobs) as (endpoint, requests):
            source = HttpActionBlobSource(endpoint, self.deadline, allow_http=True)
            with self.scope(blob_source=source.fetch) as registry:
                self.assertEqual(
                    registry.evidence(self.candidate.tree_identity), self.custody
                )
                worker = LocalStandardLifecyclePorts(
                    source_trees=registry,
                    object_root=self.root / "objects",
                    contracts=tuple(self.ports.contracts.values()),
                    tool_bindings=tuple(self.ports.tool_bindings.values()),
                    command_phases=(ComponentCommandPhase.BUILD,),
                )
                _, execution = _fixture()
                intent = worker.create(
                    execution, execution.generation_plans[0], self.candidate, (), ()
                )
                worker.accept_finalized_plan(
                    intent, self.inputs.authorization, self.plan
                )
                output = worker.build(self.plan, ())
                self.assertEqual(
                    worker.artifact_path(output.exports[0]).read_text(),
                    "known-output\n",
                )
            self.assertEqual(
                len(requests), len({item.blob.identity for item in self.files})
            )
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_bad_plan_or_candidate_refuses_before_fetch(self):
        fetch = Mock(side_effect=AssertionError("must not fetch"))
        revoked = replace(
            self.inputs.authorization,
            grant=replace(self.inputs.authorization.grant, revoked=True),
        )
        with self.assertRaisesRegex(AuthorizationError, "revoked"):
            with self.scope(
                inputs=replace(self.inputs, authorization=revoked), blob_source=fetch
            ):
                self.fail("source was admitted")
        with self.assertRaisesRegex(ActionWireError, "source authority"):
            with self.scope(
                candidate=replace(
                    self.candidate, component_revision=canonical_identity("foreign")
                ),
                blob_source=fetch,
            ):
                self.fail("source was admitted")
        fetch.assert_not_called()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_expiry_during_transfer_never_yields_source(self):
        def fetch(reference):
            self.now = self.inputs.authorization.grant.expires_at
            return self.controller_cas.get_bytes(reference)

        with self.assertRaisesRegex(AuthorizationError, "expired"):
            with self.scope(blob_source=fetch):
                self.fail("expired source was admitted")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_changed_custody_or_interrupted_body_cleans_source(self):
        with self.assertRaisesRegex(ActionWireError, "custody identity"):
            with self.scope(source_custody_identity=canonical_identity("other")):
                self.fail("changed custody was admitted")
        self.assertEqual(list(self.workspace.iterdir()), [])
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with self.scope():
                raise RuntimeError("interrupted")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_corrupt_fetch_and_expired_deadline_do_not_leave_source(self):
        with self.assertRaises(ActionWireError):
            with self.scope(blob_source=lambda reference: b"corrupt"):
                self.fail("corrupt source was admitted")
        fetch = Mock(side_effect=AssertionError("must not fetch"))
        with self.assertRaisesRegex(ActionWireError, "deadline has expired"):
            with self.scope(
                deadline=ActionDispatchDeadline(
                    datetime.now(UTC) - timedelta(seconds=1)
                ),
                blob_source=fetch,
            ):
                self.fail("expired source was admitted")
        fetch.assert_not_called()
        self.assertEqual(list(self.workspace.iterdir()), [])
