"""Production EXECUTE capture includes runtime-only provider bytes and proof."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_test_record import TestWorkerInput
from literate_ai.adapters.execute_handoff import CompletedBuildExecuteHandoff
from tests.support import fixtures_test_action_execute_providers as provider_fixture


class ExecuteHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = provider_fixture.ExecuteProviderTests()
        self.addCleanup(f.doCleanups)
        f.preserve_sources = True
        f.setUp()
        value = f.value
        self.completed = TestWorkerInput(value.build_input, value.build_result)
        self.builder = SimpleNamespace(test_handoff=Mock(return_value=self.completed))
        self.indexer = SimpleNamespace(
            execution_plan=value.build_input.execution_plan,
            cas=f.fixture.source,
            deadline=f.fixture.deadline,
            _candidate=lambda *args: value.build_input.candidate,
        )
        # The fixture uses two local ports; production retains both nodes in one.
        consumer, provider = f.consumer, f.fixture.fixture.receiver
        artifact_path = consumer.artifact_path
        records = tuple(
            sorted(
                dict(
                    consumer.retained_evidence_records()
                    + provider.retained_evidence_records()
                ).items(),
                key=lambda item: item[0].uri,
            )
        )
        self.enterContext(
            patch.object(
                consumer,
                "artifact_path",
                side_effect=lambda item: (
                    provider.artifact_path(item)
                    if item in value.provider_artifacts
                    else artifact_path(item)
                ),
            )
        )
        self.enterContext(
            patch.object(consumer, "retained_evidence_records", return_value=records)
        )
        self.handoff = CompletedBuildExecuteHandoff(
            self.builder, self.indexer, consumer
        )

    def capture(self):
        value = self.fixture.value
        return self.handoff(
            value.build_input.plan,
            value.build_result.evidence.exports,
            value.scope,
            value.provider_artifacts,
            value.accepted_providers,
        )

    def test_full_runtime_custody_preserves_empty_build_provider_closure(self):
        value = self.capture()
        self.assertEqual(value.build_input, self.fixture.value.build_input)
        self.assertEqual(value.build_result, self.fixture.value.build_result)
        self.assertEqual(value.scope, self.fixture.value.scope)
        self.assertEqual(
            value.accepted_providers, self.fixture.value.accepted_providers
        )
        self.fixture.content = value.to_bytes()
        self.fixture.execute()
        self.assertEqual(
            self.fixture.workers[-1].execution_stdout[
                value.build_input.plan.component_revision.uri
            ],
            "known-output",
        )
        self.fixture.assert_clean()
        self.assertEqual(value.build_input.accepted_providers, ())
        self.assertTrue(value.provider_builds)

    def test_changed_provider_artifact_cannot_reuse_previous_capture(self):
        self.capture()
        (self.fixture.fixture.archive_root / "app").write_text("changed")
        with self.assertRaises((ActionWireError, ValueError)):
            self.capture()

    def test_changed_build_custody_refuses_before_provider_capture(self):
        with (
            patch.object(
                self.fixture.consumer, "build_evidence_for_test", return_value=None
            ),
            patch(
                "literate_ai.adapters.execute_handoff.capture_provider_transfers",
                side_effect=AssertionError("premature capture"),
            ),
            self.assertRaises(ActionWireError),
        ):
            self.capture()

    def test_missing_runtime_receipts_refuse_before_completed_build_lookup(self):
        value = self.fixture.value
        with self.assertRaises((ActionWireError, ValueError)):
            self.handoff(
                value.build_input.plan,
                value.build_result.evidence.exports,
                value.scope,
                value.provider_artifacts,
                (),
            )
        self.builder.test_handoff.assert_not_called()
