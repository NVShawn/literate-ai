"""Generation wire custody reopens exact bounded prompt authority without host paths."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.retained_source import RetainedSourceInput
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_source_generation_scheduling import _PreparedFixture


class GenerateWorkerInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _PreparedFixture()
        self.addCleanup(self.fixture.close)
        self.prepared = self.fixture.nodes[0]
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        self.cas = FileSystemCAS(Path(self.fixture.temporary.name) / "wire-cas")
        self.value = GenerateWorkerInput.capture(
            self.fixture.execution, self.prepared, self.cas, self.deadline
        )
        self.content = self.value.to_bytes()

    def admit(self, content=None, deadline=None):
        raw = self.content if content is None else content
        return GenerateWorkerInput.admit(
            raw, record_identity(raw), deadline or self.deadline
        )

    def test_exact_request_roundtrip_reopens_prompt_without_workspace_path(self):
        admitted = self.admit()
        self.assertEqual(admitted, self.value)
        self.assertEqual(admitted.plan, self.prepared.plan)
        self.assertEqual(
            admitted.workspace_allocation_identity,
            self.prepared.workspace.allocation_identity,
        )
        self.assertNotIn(self.prepared.workspace.locator.encode(), self.content)
        prompt = admitted.read_prompt(
            self.cas.get_bytes(admitted.prompt), self.deadline
        )
        self.assertEqual(prompt, self.prepared.request)

    def test_changed_prompt_or_manifest_size_refuses(self):
        for content in (
            self.prepared.request.prompt + b"x",
            b"x" * self.value.prompt.size,
        ):
            with (
                self.subTest(content_size=len(content)),
                self.assertRaises(ActionWireError),
            ):
                self.value.read_prompt(content, self.deadline)
        changed = replace(
            self.value,
            prompt=replace(self.value.prompt, size=self.value.prompt.size + 1),
        )
        with self.assertRaises(ActionWireError):
            self.admit(changed.to_bytes())

    def test_foreign_plan_or_prompt_identity_refuses(self):
        other = next(
            node for node in self.fixture.nodes if node.plan != self.prepared.plan
        )
        for changed in (
            replace(self.value, generation_plan_identity=other.plan.identity),
            replace(self.value, generation_plan_identity=canonical_identity("unknown")),
            replace(self.value, prompt=replace(self.value.prompt, digest="0" * 64)),
        ):
            with (
                self.subTest(value=changed.generation_plan_identity),
                self.assertRaises(ActionWireError),
            ):
                self.admit(changed.to_bytes())

    def test_recomputed_envelope_cannot_forge_budget_measurement(self):
        request = self.value.request
        decision = replace(
            request.budget_decision,
            estimated_tokens=request.budget_decision.estimated_tokens + 1,
        )
        changed = replace(
            self.value,
            request=replace(
                request,
                budget_decision=decision,
                complexity_decision_identity=decision.identity,
            ),
        )
        admitted = self.admit(changed.to_bytes())
        with self.assertRaises(ActionWireError):
            admitted.read_prompt(self.prepared.request.prompt, self.deadline)

    def test_closed_canonical_envelope_and_input_hash(self):
        doc = json.loads(self.content)
        for raw in (
            json.dumps(doc, indent=2).encode(),
            canonical_json_bytes(doc | {"workspace": "/foreign/path"}),
            b'{"schema":"foreign",' + self.content[1:],
            b"[]",
        ):
            with self.subTest(raw=raw[:40]), self.assertRaises(ActionWireError):
                self.admit(raw)
        with self.assertRaises(ActionWireError):
            GenerateWorkerInput.admit(
                self.content, canonical_identity("wrong"), self.deadline
            )

    def test_deadline_is_checked_on_envelope_and_prompt_read(self):
        expired = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        with self.assertRaises(ActionWireError):
            self.admit(deadline=expired)
        with self.assertRaises(ActionWireError):
            self.value.read_prompt(self.prepared.request.prompt, expired)

    def retained(self, lock=None):
        root = Path(self.fixture.temporary.name) / "retained"
        root.mkdir(exist_ok=True)
        (root / "main.py").write_bytes(b"pass\n")
        return RetainedSourceInput.capture(
            root.resolve(),
            component_lock_identity=lock
            or self.fixture.execution.component_lock_identity,
            project_authority_identity=canonical_identity("project"),
            target="host",
        )

    def test_retained_request_uses_version_two_and_exact_review(self):
        retained = self.retained()
        value = GenerateWorkerInput.capture(
            self.fixture.execution,
            self.prepared,
            self.cas,
            self.deadline,
            retained=retained,
            authorization=retained.identity.uri,
        )
        self.assertEqual(
            json.loads(value.to_bytes())["schema"],
            "literate-ai/generate-worker-input@2",
        )
        self.assertEqual(self.admit(value.to_bytes()), value)
        self.assertEqual(value.retained.authorization, retained.identity)
        self.assertEqual(
            value.retained.read_files(self.cas.get_bytes, self.deadline), retained.files
        )
        self.assertEqual(
            json.loads(self.content)["schema"], "literate-ai/generate-worker-input@1"
        )

    def test_retained_foreign_lock_cannot_enter_execution_plan(self):
        retained = self.retained(canonical_identity("foreign-lock"))
        with self.assertRaises(ActionWireError):
            GenerateWorkerInput.capture(
                self.fixture.execution,
                self.prepared,
                self.cas,
                self.deadline,
                retained=retained,
                authorization=retained.identity.uri,
            )

    def test_version_two_requires_retained_and_version_one_forbids_it(self):
        original = json.loads(self.content)
        for doc in (
            original | {"schema": "literate-ai/generate-worker-input@2"},
            original | {"retained": None},
            original
            | {"schema": "literate-ai/generate-worker-input@2", "retained": None},
        ):
            with self.subTest(doc=doc["schema"]), self.assertRaises(ActionWireError):
                self.admit(canonical_json_bytes(doc))
