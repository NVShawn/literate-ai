"""LINK descriptors do not substitute for acceptance proof and artifact bytes."""

import unittest
from dataclasses import replace

import tests.support.fixtures_test_action_link_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_link_proof import reopen_link_component
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.storage import FileSystemCAS, StorageError


class LinkComponentProofTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.LinkWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.value = f.value
        self.source = f.fixture.worker.cas
        self.cas = FileSystemCAS(self.source.root.parent / "link-cas")

    def reopen(self, value=None, **changes):
        raw = (value or self.value).to_bytes()
        arguments = dict(
            input_record=raw,
            input_identity=record_identity(raw),
            cas=self.cas,
            deadline=self.fixture.deadline,
            admission_guard=lambda: None,
            blob_source=self.source.get_bytes,
        )
        arguments.update(changes)
        return reopen_link_component(**arguments)

    def test_reopens_accepted_proof_and_archive_from_separate_cas(self):
        manifest, files, reader = self.reopen()
        self.assertEqual(manifest, self.value.manifest)
        self.assertTrue(files)
        self.assertEqual(
            reader.read_json(self.value.acceptance_result.evidence.identity),
            self.value.acceptance_result.evidence.to_dict(),
        )

    def test_missing_supporting_record_refuses_hash_valid_descriptor(self):
        result = self.value.acceptance_result
        omitted = result.evidence.build.source_sbom.bom_identity.uri
        refs = tuple(ref for ref in result.evidence_records if ref.identity != omitted)
        self.assertLess(len(refs), len(result.evidence_records))
        changed = replace(
            self.value, acceptance_result=replace(result, evidence_records=refs)
        )
        with self.assertRaises((QualificationCaptureError, ActionWireError)):
            self.reopen(changed)

    def test_corrupt_archive_refuses_even_when_acceptance_records_are_valid(self):
        archive = (
            self.value.acceptance_input.execution_input.build_result.artifact_archive
        )

        def fetch(ref):
            return b"corrupt" if ref == archive else self.source.get_bytes(ref)

        with self.assertRaises((ActionWireError, StorageError)):
            self.reopen(blob_source=fetch)

    def test_authority_loss_during_transfer_refuses(self):
        healthy = True

        def guard():
            if not healthy:
                raise RuntimeError("authority lost")

        def fetch(ref):
            nonlocal healthy
            healthy = False
            return self.source.get_bytes(ref)

        with self.assertRaisesRegex(RuntimeError, "authority lost"):
            self.reopen(admission_guard=guard, blob_source=fetch)
