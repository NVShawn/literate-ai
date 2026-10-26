"""Actual command transport and CAS custody; no native target is inferred."""

from __future__ import annotations

import base64
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_builder import CommandComponentBuilder
from literate_ai.application.action_dag_scheduler import LifecycleActionTermination
from literate_ai.contracts import canonical_identity
from literate_ai.storage.cas import BlobIntegrityError
from tests.critical import test_command_builder as builder_fixture


class CoalescingCommandBuildTests(unittest.TestCase):
    def setUp(self):
        fixture = builder_fixture.CommandBuilderTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.builder = CommandComponentBuilder(
            fixture.indexer,
            fixture.admission,
            fixture.ports,
            result_source=fixture.fetch,
            coalesce_artifacts=True,
        )

    def records(self, schema):
        return [
            value
            for _, content in self.fixture.ports.retained_evidence_records()
            if isinstance(value := json.loads(content), dict)
            and value.get("schema") == schema
        ]

    def test_exact_input_rerun_avoids_command_and_reopens_fresh_admission(self):
        fixture = self.fixture
        with patch.object(
            fixture.ports, "build", side_effect=AssertionError("local fallback")
        ):
            first = self.builder.build(fixture.plan, ())
            fixture.marker.unlink()  # Only this test-owned command marker.
            self.builder.nodes[fixture.plan.component_revision] = replace(
                self.builder.nodes[fixture.plan.component_revision],
                cache_affinity_worker_ids=("builder",),
            )
            second = self.builder.build(fixture.plan, ())
        self.assertFalse(fixture.marker.exists())
        self.assertEqual(first.exports, second.exports)
        self.assertEqual(len(self.records("literate-ai/command-build-attempt@1")), 1)
        admissions = self.records("literate-ai/command-build-artifact-admission@1")
        self.assertEqual(len(admissions), 2)
        self.assertNotEqual(
            admissions[0]["request_identity"], admissions[1]["request_identity"]
        )
        self.assertEqual(
            admissions[0]["build_evidence_identity"], first.evidence.identity.uri
        )
        self.assertEqual(
            self.builder.test_handoff(
                fixture.plan, second.exports
            ).build_result.evidence,
            second.evidence,
        )

    def test_command_process_failure_is_retained_and_explicit_retry_works(self):
        fixture = self.fixture
        expected_input = fixture.root / "expected-input.json"
        expected_input.unlink()  # This test's receiver now exits nonzero.
        with self.assertRaises(ActionWireError):
            self.builder.build(fixture.plan, ())
        expected_input.write_bytes(fixture.fixture.input_record)
        attempts = self.records("literate-ai/command-build-attempt@1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "failed")
        self.assertEqual(attempts[0]["failure_code"], "action_dispatch.failed")
        diagnostics = self.records("literate-ai/action-command-failure@1")
        self.assertEqual(len(diagnostics), 1)
        self.assertNotEqual(diagnostics[0]["exit_status"], 0)
        self.assertIn(
            b"FileNotFoundError", base64.b64decode(diagnostics[0]["stderr_base64"])
        )
        self.assertFalse(fixture.marker.exists())
        # This fixture owns and observes its synchronous local receiver. Generic
        # command/SSH transport alone may not assert remote terminal ownership.
        pending = fixture.indexer.slots.unresolved_ownership[0]
        termination = LifecycleActionTermination(
            pending.identity,
            pending.worker.worker_identity,
            canonical_identity("fixture-owner-terminal"),
            "owned-terminal",
            diagnostics[0]["exit_status"],
        )
        key, _ = self.builder._artifact_dispatcher.unresolved_productions[0]
        fixture.indexer.slots.resolve_ownership(termination)
        self.builder._artifact_dispatcher.resolve_production(key, termination)
        self.builder.build(fixture.plan, ())
        self.assertTrue(fixture.marker.exists())
        self.assertEqual(len(self.records("literate-ai/command-build-attempt@1")), 2)

    def test_corrupt_cached_archive_refuses_without_another_command_or_admission(self):
        fixture = self.fixture
        self.builder.build(fixture.plan, ())
        fixture.marker.unlink()
        handoff = self.builder.test_handoff(
            fixture.plan, fixture.fixture.fixture.output.exports
        )
        archive = fixture.fixture.cas.path_for(handoff.build_result.artifact_archive)
        archive.chmod(0o600)
        archive.write_bytes(b"corrupt test-owned CAS archive")
        with self.assertRaises(BlobIntegrityError):
            self.builder.build(fixture.plan, ())
        self.assertFalse(fixture.marker.exists())
        self.assertEqual(
            len(self.records("literate-ai/command-build-artifact-admission@1")), 1
        )

    def test_worker_change_refuses_cached_payload_before_new_admission(self):
        fixture = self.fixture
        self.builder.build(fixture.plan, ())
        fixture.marker.unlink()
        fixture.healthy = False
        with self.assertRaises(ActionWireError):
            self.builder.build(fixture.plan, ())
        self.assertFalse(fixture.marker.exists())
        self.assertEqual(
            len(self.records("literate-ai/command-build-artifact-admission@1")), 1
        )


if __name__ == "__main__":
    unittest.main()
