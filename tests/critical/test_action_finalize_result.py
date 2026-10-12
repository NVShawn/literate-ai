"""Returned FINALIZE bytes remain untrusted until private stage verification passes."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import tests.support.fixtures_test_action_finalize_result_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_result import import_finalize_result
from literate_ai.contracts import canonical_json_bytes
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import BlobIntegrityError


class FinalizeImportTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.FinalizeResultTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.cas = FileSystemCAS(root / "receiver")
        self.sender = FileSystemCAS(root / "sender")
        records = [canonical_json_bytes(name) for name in ("test", "execute", "accept")]
        records.append(canonical_json_bytes(f.result.evidence.to_dict()))
        self.result = replace(
            f.result,
            evidence_records=tuple(
                sorted(
                    (self.sender.put_bytes(raw) for raw in records),
                    key=lambda ref: ref.identity,
                )
            ),
        )
        self.stage_verifier = Mock()
        self.guard = Mock()
        self.fetch = Mock(side_effect=self.sender.get_bytes)
        # Isolate return transport; full predecessor proof is tested by the chain.
        self.reopen = self.enterContext(
            patch(
                "literate_ai.adapters.action_finalize_result.reopen_finalize_input",
                return_value=(f.value, f.fixture.package),
            )
        )

    def receive(self, **changes):
        raw = self.result.to_bytes()
        args = dict(
            content=raw,
            result_identity=record_identity(raw),
            input_record=self.fixture.raw,
            input_identity=self.fixture.identity,
            records={},
            cas=self.cas,
            deadline=self.fixture.fixture.deadline,
            admission_guard=self.guard,
            verify_package=Mock(),
            verify_stages=self.stage_verifier,
            blob_source=self.fetch,
        )
        args.update(changes)
        return import_finalize_result(**args)

    def test_fetches_exact_records_and_verifies_immutable_snapshot(self):
        self.assertEqual(self.receive(), self.result.evidence)
        value, evidence, records = self.stage_verifier.call_args.args
        self.assertEqual(value, self.fixture.value)
        self.assertEqual(evidence, self.result.evidence)
        self.assertEqual(len(records), 4)
        for identity, raw in records.items():
            self.assertEqual(record_identity(raw), identity)
        with self.assertRaises(TypeError):
            records[evidence.identity] = b"changed"
        self.assertEqual(self.fetch.call_count, 4)

    def test_missing_private_verifier_never_reads_proof(self):
        with self.assertRaises(TypeError):
            self.receive(verify_stages=None)
        self.reopen.assert_not_called()
        self.fetch.assert_not_called()

    def test_warm_cache_still_requires_private_verification(self):
        for ref in self.result.evidence_records:
            self.cas.put_bytes(self.sender.get_bytes(ref))
        self.assertEqual(self.receive(blob_source=None), self.result.evidence)
        self.stage_verifier.assert_called_once()
        self.fetch.assert_not_called()

    def test_corrupt_cache_is_not_repaired_by_remote_fallback(self):
        ref = self.result.evidence_records[0]
        self.cas.put_bytes(self.sender.get_bytes(ref))
        path = self.cas.path_for(ref)
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
        with self.assertRaises(BlobIntegrityError):
            self.receive()
        self.fetch.assert_not_called()
        self.stage_verifier.assert_not_called()

    def test_corrupt_returned_record_refuses_before_stage_verifier(self):
        self.fetch.side_effect = lambda ref: b"corrupt"
        with self.assertRaises(ActionWireError) as error:
            self.receive()
        self.assertEqual(error.exception.code, "action_finalize.record_invalid")
        self.stage_verifier.assert_not_called()

    def test_upstream_rejection_prevents_evidence_fetch(self):
        self.reopen.side_effect = ActionWireError("fixture.proof", "invalid proof")
        with self.assertRaises(ActionWireError):
            self.receive()
        self.fetch.assert_not_called()
        self.stage_verifier.assert_not_called()

    def test_private_verifier_rejection_is_not_success(self):
        self.stage_verifier.side_effect = ValueError("oracle differs")
        with self.assertRaisesRegex(ValueError, "oracle differs"):
            self.receive()

    def test_revocation_after_fetch_stops_before_cas_write(self):
        revoked = False

        def fetch(ref):
            nonlocal revoked
            revoked = True
            return self.sender.get_bytes(ref)

        def guard():
            if revoked:
                raise PermissionError("revoked")

        with self.assertRaisesRegex(PermissionError, "revoked"):
            self.receive(blob_source=fetch, admission_guard=guard)
        self.stage_verifier.assert_not_called()
        self.assertEqual(list(self.cas.iter_refs()), [])

    def test_revocation_during_verification_prevents_return(self):
        def verify(*args):
            self.guard.side_effect = PermissionError("revoked")

        with self.assertRaisesRegex(PermissionError, "revoked"):
            self.receive(verify_stages=verify)
