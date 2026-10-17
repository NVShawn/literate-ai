"""Rehashed LINK results cannot change accepted manifests or dependency custody."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import tests.support.fixtures_test_action_link_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_link_result import LinkWorkerResult
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class LinkWorkerResultTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.LinkWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.raw = f.value.to_bytes()
        self.identity = record_identity(self.raw)
        self.result = LinkWorkerResult.expected(self.raw, self.identity, f.deadline)

    def admit(self, raw=None, deadline=None):
        raw = self.result.to_bytes() if raw is None else raw
        return LinkWorkerResult.admit(
            raw,
            record_identity(raw),
            input_record=self.raw,
            input_identity=self.identity,
            deadline=deadline or self.fixture.deadline,
        )

    def test_exact_manifest_and_acceptance_result_roundtrip(self):
        self.assertEqual(self.admit(), self.result)
        self.assertEqual(self.result.manifest, self.fixture.value.manifest)
        self.assertEqual(
            self.result.acceptance_result_identity,
            record_identity(self.fixture.value.acceptance_result.to_bytes()),
        )

    def test_rehashed_origin_or_dependency_substitution_refuses(self):
        for changed in (
            replace(self.result, input_identity=canonical_identity("other-input")),
            replace(
                self.result,
                acceptance_result_identity=canonical_identity("other-acceptance"),
            ),
            replace(
                self.result,
                dependency_links=(("other-link", canonical_identity("other-result")),),
            ),
        ):
            with (
                self.subTest(changed=changed.input_identity),
                self.assertRaises(ActionWireError),
            ):
                self.admit(changed.to_bytes())

    def test_rehashed_manifest_change_and_unknown_fields_refuse(self):
        original = json.loads(self.result.to_bytes())
        changed = json.loads(self.result.to_bytes())
        changed["manifest"]["component_revision"] = canonical_identity(
            "foreign"
        ).to_dict()
        for doc in (changed, original | {"skip_verification": True}):
            with self.assertRaises(ActionWireError):
                self.admit(canonical_json_bytes(doc))
        with self.assertRaises(ActionWireError):
            self.admit(self.result.to_bytes() + b"\n")

    def test_expired_result_refuses(self):
        expired = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        with self.assertRaises(ActionWireError):
            self.admit(deadline=expired)
