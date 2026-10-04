"""Shared test fixtures extracted from test_action_provider_build."""

import shutil
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.action_provider_build import (
    capture_provider_build,
    read_provider_build,
)
from literate_ai.adapters.action_provider_record import (
    ProviderBuildTransfer,
    validate_provider_transfers,
)
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.generated_tests import GeneratedTestSuiteError
from literate_ai.storage import FileSystemCAS
from tests.support import fixtures_test_standard_transferred_build as transfer_fixture
from tests.support.action_deadline import ACTION_TEST_DEADLINE
from tests.support.fixtures_test_action_build_intent import provider_evidence
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class ProviderBuildTransferTests(unittest.TestCase):
    def setUp(self):
        fixture = transfer_fixture.StandardTransferredBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        ports = fixture.receiver
        ports.retain_evidence_with(fixture.recorder)
        fixture.admit()
        tests = ports.test(fixture.plan, fixture.output.exports)
        execution = ports.execute(fixture.plan, fixture.output.exports)
        self.receipt = ports.accept(fixture.plan, tests.identity, execution.identity)
        self.archive_root = ports.artifact_path(fixture.output.exports[0]).parent
        self.source = FileSystemCAS(fixture.root / "provider-cas")
        self.target = FileSystemCAS(fixture.root / "consumer-cas")
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + ACTION_TEST_DEADLINE)
        self.guard = Mock()
        self.validation = ports.source_trees.validation_inputs(
            self.receipt.build.source_tree_identity
        )
        self.generation_plan = next(
            item
            for item in _fixture()[1].generation_plans
            if item.component_revision == self.receipt.component_revision
        )
        self.transfer = capture_provider_build(
            receipt=self.receipt,
            artifact_root=self.archive_root,
            records=fixture.recorder.entries,
            cas=self.source,
            deadline=self.deadline,
            require_current=self.guard,
            source_validation=self.validation,
            generation_plan=self.generation_plan,
        )

    def read(self, **changes):
        args = dict(
            transfer=self.transfer,
            receipt=self.receipt,
            cas=self.target,
            deadline=self.deadline,
            require_current=self.guard,
            blob_source=self.source.get_bytes,
            generation_plan=self.generation_plan,
        )
        args.update(changes)
        return read_provider_build(**args)

    def test_unrelated_retained_records_do_not_change_provider_transfer(self):
        from literate_ai.contracts import canonical_json_bytes

        content = canonical_json_bytes({"unrelated": "later dispatch record"})
        identity = canonical_identity({"unrelated": "later dispatch record"})
        records = tuple(
            sorted(
                (*self.fixture.recorder.entries, (identity, content)),
                key=lambda item: item[0].uri,
            )
        )
        transfer = capture_provider_build(
            receipt=self.receipt,
            artifact_root=self.archive_root,
            records=records,
            cas=self.source,
            deadline=self.deadline,
            require_current=self.guard,
            source_validation=self.validation,
            generation_plan=self.generation_plan,
        )
        self.assertEqual(transfer, self.transfer)
        self.assertNotIn(
            identity.uri, {ref.identity for ref in transfer.evidence_records}
        )
        self.assertTrue(self.read(transfer=transfer)[0])

    def test_transfer_descriptor_round_trip_and_exact_membership(self):
        parsed = ProviderBuildTransfer.from_dict(self.transfer.to_dict())
        self.assertEqual(parsed, self.transfer)
        validate_provider_transfers((self.receipt,), (parsed,))
        self.assertTrue(self.read(transfer=parsed)[0])
        for transfers in ((), (parsed, parsed)):
            with self.assertRaises(ActionWireError):
                validate_provider_transfers((self.receipt,), transfers)
        with self.assertRaises(ActionWireError):
            ProviderBuildTransfer.from_dict(
                {**parsed.to_dict(), "private_path": "refused"}
            )
        with self.assertRaises(ActionWireError):
            validate_provider_transfers(
                (self.receipt,),
                (replace(parsed, evidence_records=parsed.evidence_records[::-1]),),
            )

    def test_action_wide_unique_evidence_budget_is_enforced(self):
        second = provider_evidence(canonical_identity("second-provider"))
        receipts = (self.receipt, second)
        transfers = []
        for receipt in receipts:
            identities = {
                receipt.identity,
                receipt.build.identity,
                receipt.build.build_plan_identity,
                receipt.generated_tests.identity,
                receipt.execution.identity,
                receipt.acceptance_policy_identity,
                receipt.build.source_custody_identity,
            }
            refs = tuple(
                sorted(
                    (BlobRef(identity.digest, 1) for identity in identities),
                    key=lambda item: item.identity,
                )
            )
            transfers.append(
                ProviderBuildTransfer(
                    receipt.identity,
                    self.transfer.artifact_archive,
                    refs,
                    self.validation,
                )
            )
        with patch(
            "literate_ai.adapters.action_provider_record.MAX_BUILD_EVIDENCE_RECORDS", 8
        ):
            for receipt, transfer in zip(receipts, transfers, strict=True):
                transfer.require_receipt(receipt)
            with self.assertRaises(ActionWireError):
                validate_provider_transfers(receipts, tuple(transfers))

    def test_shared_digest_cannot_change_metadata_between_archive_and_proof(self):
        conflicting = replace(
            self.transfer.artifact_archive, media_type="application/x-conflict"
        )
        transfer = replace(
            self.transfer,
            evidence_records=tuple(
                sorted(
                    (*self.transfer.evidence_records, conflicting),
                    key=lambda item: item.identity,
                )
            ),
        )
        with self.assertRaises(ActionWireError):
            validate_provider_transfers((self.receipt,), (transfer,))
        fetch = Mock(side_effect=AssertionError("unexpected fetch"))
        with self.assertRaises(ActionWireError):
            self.read(transfer=transfer, blob_source=fetch)
        fetch.assert_not_called()

    def test_missing_acceptance_or_execution_process_proof_refuses(self):
        # Removing the exact accepted execution record must refuse before fetch.
        refs = tuple(
            ref
            for ref in self.transfer.evidence_records
            if ref.identity != self.receipt.execution.identity.uri
        )
        fetch = Mock(side_effect=AssertionError("unexpected fetch"))
        with self.assertRaises(ActionWireError):
            self.read(
                transfer=replace(self.transfer, evidence_records=refs),
                blob_source=fetch,
            )
        fetch.assert_not_called()
        # Capture must reopen the acceptance policy record as well.
        records = tuple(
            (identity, content)
            for identity, content in self.fixture.recorder.entries
            if identity != self.receipt.acceptance_policy_identity
        )
        with self.assertRaises(QualificationCaptureError):
            capture_provider_build(
                receipt=self.receipt,
                artifact_root=self.archive_root,
                records=records,
                cas=self.source,
                deadline=self.deadline,
                require_current=self.guard,
                source_validation=self.validation,
                generation_plan=self.generation_plan,
            )

    def test_underlying_test_and_execution_observations_are_required(self):
        for missing in (
            self.receipt.generated_tests.cases[0].observation_identity,
            self.receipt.execution.observation_identity,
        ):
            with self.subTest(missing=missing.uri):
                refs = tuple(
                    ref
                    for ref in self.transfer.evidence_records
                    if ref.identity != missing.uri
                )
                self.assertLess(len(refs), len(self.transfer.evidence_records))
                with self.assertRaises(QualificationCaptureError):
                    self.read(transfer=replace(self.transfer, evidence_records=refs))

    def test_changed_provider_plan_or_recipe_authority_refuses(self):
        with self.assertRaises(ActionWireError):
            self.read(
                generation_plan=replace(
                    self.generation_plan,
                    component_graph_identity=canonical_identity("other-graph"),
                )
            )
        changed = replace(
            self.validation, recipe_identity=canonical_identity("other-recipe").uri
        )
        with self.assertRaises(GeneratedTestSuiteError):
            self.read(transfer=replace(self.transfer, source_validation=changed))

    def test_reopens_actual_accepted_build_after_provider_files_are_removed(self):
        shutil.rmtree(self.archive_root)
        with patch.object(
            self.fixture.producer,
            "build",
            side_effect=AssertionError("provider rebuild"),
        ):
            files, reader = self.read()
        self.assertEqual(
            next(item.content for item in files if item.path == "app"),
            b"known-output\n",
        )
        self.assertEqual(
            reader.read_json(self.receipt.build.build_plan_identity),
            self.fixture.plan.to_dict(),
        )
        self.assertGreater(self.guard.call_count, 2)

    def test_historical_provider_grant_may_expire_before_consumer_read(self):
        future = self.fixture.inputs.authorization.grant.expires_at + timedelta(
            seconds=1
        )
        deadline = ActionDispatchDeadline(future + timedelta(minutes=1))
        with patch(
            "literate_ai.adapters.action_dispatch_wire.datetime", wraps=datetime
        ) as clock:
            clock.now.return_value = future
            files, _ = self.read(deadline=deadline)
        self.assertTrue(files)
        self.guard.assert_called()

    def test_foreign_receipt_and_corrupt_bytes_refuse(self):
        fetch = Mock(side_effect=AssertionError("unexpected fetch"))
        with self.assertRaises(ActionWireError):
            self.read(
                transfer=replace(
                    self.transfer, receipt_identity=canonical_identity("foreign")
                ),
                blob_source=fetch,
            )
        fetch.assert_not_called()
        with self.assertRaises(ActionWireError):
            self.read(blob_source=lambda ref: b"corrupt")

    def test_consumer_authority_revocation_refuses_before_fetch(self):
        fetch = Mock(side_effect=AssertionError("unexpected fetch"))
        guard = Mock(side_effect=RuntimeError("consumer grant expired"))
        with self.assertRaisesRegex(RuntimeError, "consumer grant expired"):
            self.read(require_current=guard, blob_source=fetch)
        fetch.assert_not_called()
