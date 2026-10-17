"""Worker source proof is verified completely before controller CAS publication."""

import unittest
from dataclasses import replace
from unittest.mock import patch

import tests.support.fixtures_test_action_generate_result as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_generate_import import import_generate_result
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.contracts import ContentIdentity
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import BlobNotFoundError


class GenerateImportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.GenerateWorkerResultTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.cas = FileSystemCAS(f.fixture.root / "controller")
        self.healthy = True
        self.fetched = []

    def guard(self):
        if not self.healthy:
            raise ActionWireError("fixture.revoked", "admission changed")

    def fetch(self, ref):
        self.fetched.append(ref)
        return self.fixture.fixture.cas.get_bytes(ref)

    def run_import(self, **changes):
        f = self.fixture
        content = f.result.to_bytes()
        return import_generate_result(
            **(
                dict(
                    content=content,
                    result_identity=record_identity(content),
                    input_record=f.input,
                    input_identity=f.identity,
                    deadline=f.deadline,
                    cas=self.cas,
                    admission_guard=self.guard,
                    blob_source=self.fetch,
                )
                | changes
            )
        )

    def test_separate_cas_import_reopens_full_source_proof_and_reuses_local_bytes(self):
        result = self.run_import()
        self.assertEqual(result, self.fixture.result)
        self.assertEqual(tuple(self.fetched), result.evidence_records)
        result.verify_records(
            tuple(
                (ContentIdentity.parse_uri(ref.identity), self.cas.get_bytes(ref))
                for ref in result.evidence_records
            )
        )
        self.fetched.clear()
        self.assertEqual(self.run_import(blob_source=None), result)
        self.assertEqual(self.fetched, [])

    def test_missing_explicit_transport_refuses_without_publication(self):
        with self.assertRaises(BlobNotFoundError):
            self.run_import(blob_source=None)
        self.assertEqual(tuple(self.cas.iter_refs()), ())

    def test_last_corrupt_blob_does_not_publish_earlier_fetched_bytes(self):
        last = self.fixture.result.evidence_records[-1]

        def fetch(ref):
            return b"corrupt" if ref == last else self.fetch(ref)

        with self.assertRaises(ActionWireError):
            self.run_import(blob_source=fetch)
        self.assertEqual(tuple(self.cas.iter_refs()), ())
        self.assertTrue(self.fetched)

    def test_hash_valid_incomplete_proof_refuses_before_publication(self):
        result = self.fixture.result
        missing = result.output.candidate.source_manifest_identity.uri
        result = replace(
            result,
            evidence_records=tuple(
                ref for ref in result.evidence_records if ref.identity != missing
            ),
        )
        content = result.to_bytes()
        with self.assertRaises(QualificationCaptureError):
            self.run_import(content=content, result_identity=record_identity(content))
        self.assertEqual(tuple(self.cas.iter_refs()), ())

    def test_authority_loss_during_fetch_refuses_without_publication(self):
        def fetch(ref):
            self.healthy = False
            return self.fetch(ref)

        with self.assertRaises(ActionWireError) as error:
            self.run_import(blob_source=fetch)
        self.assertEqual(error.exception.code, "fixture.revoked")
        self.assertEqual(tuple(self.cas.iter_refs()), ())

    def test_corrupt_local_custody_does_not_fall_back_to_transport(self):
        with patch.object(self.cas, "get_bytes", side_effect=ValueError("corrupt")):
            with self.assertRaises(ValueError):
                self.run_import()
        self.assertEqual(self.fetched, [])

    def test_storage_failure_cannot_return_admitted_result(self):
        with patch.object(self.cas, "put_bytes", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                self.run_import()
        self.assertEqual(tuple(self.cas.iter_refs()), ())
