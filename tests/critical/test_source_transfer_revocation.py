"""Shared test fixtures extracted from test_action_build_source."""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.action_build_source import materialize_build_source
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.source_evidence_validation import (
    SourceEvidenceValidationInputs,
)
from literate_ai.contracts.generation_cache import CachedSourceFile
from literate_ai.storage import FileSystemCAS
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_standard_local_command_adapter import (
    _python_copy_lifecycle,
)


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

    def test_revocation_during_fetch_stops_before_source_admission(self):
        revoked = False

        def guard():
            if revoked:
                raise ActionWireError("fixture.revoked", "source admission revoked")

        def fetch(reference):
            nonlocal revoked
            content = self.controller_cas.get_bytes(reference)
            revoked = True
            return content

        with self.assertRaises(ActionWireError) as error:
            with self.scope(blob_source=fetch, admission_guard=guard):
                self.fail("revoked source was admitted")
        self.assertEqual(error.exception.code, "fixture.revoked")
        self.assertEqual(list(self.workspace.iterdir()), [])
