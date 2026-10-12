"""LINK requires exact accepted exports and only its production DAG predecessors."""

import json
import unittest
from dataclasses import replace

import tests.support.fixtures_test_action_accept_result as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class LinkWorkerInputTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.AcceptWorkerResultTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.deadline = f.worker.deadline
        self.value = LinkWorkerInput(f.value, f.result)

    def admit(self, raw=None):
        raw = self.value.to_bytes() if raw is None else raw
        return LinkWorkerInput.admit(raw, record_identity(raw), self.deadline)

    def test_exact_acceptance_reopens_manifest_without_project_wide_barrier(self):
        admitted = self.admit()
        self.assertEqual(admitted, self.value)
        self.assertEqual(
            admitted.manifest.exports, self.fixture.result.evidence.build.exports
        )
        self.assertEqual(admitted.dependency_links, ())

    def test_foreign_acceptance_input_cannot_be_rehashed_into_link_input(self):
        changed = replace(
            self.value,
            acceptance_result=replace(
                self.value.acceptance_result,
                input_identity=canonical_identity("foreign"),
            ),
        )
        with self.assertRaises(ActionWireError):
            self.admit(changed.to_bytes())

    def test_extra_dependency_cannot_create_project_wide_wait(self):
        changed = replace(
            self.value,
            dependency_links=(("unrelated-link", canonical_identity("result")),),
        )
        with self.assertRaises(ActionWireError):
            self.admit(changed.to_bytes())

    def test_closed_canonical_envelope_and_hash(self):
        raw = self.value.to_bytes()
        for changed in (
            raw + b"\n",
            b"[]",
            canonical_json_bytes(json.loads(raw) | {"policy": "skip"}),
        ):
            with self.subTest(raw=changed[:20]), self.assertRaises(ActionWireError):
                self.admit(changed)
        with self.assertRaises(ActionWireError):
            LinkWorkerInput.admit(raw, canonical_identity("foreign"), self.deadline)
