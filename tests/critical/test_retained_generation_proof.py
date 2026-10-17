"""Retained generation proves exact reviewed input without invented model evidence."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    record_identity,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.action_generate_result import capture_generate_result
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    verify_qualification_generation_records,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from tests.support import (
    fixtures_test_cached_coding_cli_source_generation_runner as fixture_module,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class RetainedGenerationProofTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.CachedCodingCliSourceGenerationRunnerTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        self.retained, _ = f._retained_input()
        deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=2))
        _, execution = _fixture()
        value = GenerateWorkerInput.capture(
            execution,
            f.node,
            f.cas,
            deadline,
            retained=self.retained,
            authorization=self.retained.identity.uri,
        )
        raw = value.to_bytes()
        self.output = f.runner(f.node)
        self.result = capture_generate_result(
            input_record=raw,
            input_identity=record_identity(raw),
            output=self.output,
            cas=f.cas,
            deadline=deadline,
        )
        self.records = tuple(
            (ContentIdentity.parse_uri(ref.identity), f.cas.get_bytes(ref))
            for ref in self.result.evidence_records
        )

    def test_complete_proof_reopens_without_model_execution_or_route_claims(self):
        self.result.verify_records(self.records)
        self.fixture.generator.generate.assert_not_called()
        provenance = self.output.provenance
        self.assertEqual(provenance.retained_source_identity, self.retained.identity)
        self.assertEqual(provenance.model_stage_output_identities, ())
        self.assertEqual(provenance.route_decision_identities, ())
        self.assertEqual(provenance.provider_evidence_identities, ())
        self.assertEqual(
            self.output.candidate.tree_identity, self.retained.tree_identity
        )

    def test_retained_proof_still_requires_invocation_and_request(self):
        for identity in (
            self.fixture.invocation.identity,
            canonical_identity(self.fixture.stage_request),
        ):
            with self.subTest(identity=identity):
                records = tuple(item for item in self.records if item[0] != identity)
                self.assertLess(len(records), len(self.records))
                with self.assertRaises(QualificationCaptureError):
                    verify_qualification_generation_records(
                        QualificationEvidenceReader(
                            records,
                            max_bytes=MAX_BUILD_EVIDENCE_BYTES,
                            max_records=MAX_BUILD_EVIDENCE_RECORDS,
                        ),
                        source_output=self.output,
                    )

    def test_hash_valid_review_cannot_claim_another_tree_lock_or_extra_authority(self):
        for field in ("tree_identity", "component_lock_identity", "extra"):
            with self.subTest(field=field):
                stage = self.retained.to_dict() | {
                    field: canonical_identity("foreign").to_dict()
                }
                stage_ref = self.fixture.cas.put_bytes(canonical_json_bytes(stage))
                stage_id = ContentIdentity.parse_uri(stage_ref.identity)
                manifest = json.loads(
                    dict(self.records)[self.output.candidate.source_manifest_identity]
                )
                manifest["stage_output_record"] = stage_ref.to_dict()
                manifest["planned_coding_cli_request_identity"] = stage_id.to_dict()
                manifest_id = canonical_identity(manifest)
                candidate = replace(
                    self.output.candidate,
                    source_manifest_identity=manifest_id,
                    planned_coding_cli_request_identity=stage_id,
                )
                provenance = replace(
                    self.output.provenance,
                    candidate_identity=candidate.identity,
                    retained_source_identity=stage_id,
                    planned_coding_cli_request_identity=stage_id,
                )
                output = replace(
                    self.output,
                    candidate=candidate,
                    provenance=provenance,
                    candidate_identity=candidate.identity,
                    provenance_identity=provenance.identity,
                )
                records = dict(self.records)
                records[stage_id] = canonical_json_bytes(stage)
                records[manifest_id] = canonical_json_bytes(manifest)
                reader = QualificationEvidenceReader(
                    tuple(sorted(records.items(), key=lambda item: item[0].uri)),
                    max_bytes=MAX_BUILD_EVIDENCE_BYTES,
                    max_records=MAX_BUILD_EVIDENCE_RECORDS,
                )
                with self.assertRaises(QualificationCaptureError):
                    verify_qualification_generation_records(
                        reader, source_output=output
                    )
