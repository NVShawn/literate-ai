"""FINALIZE result descriptors reject substituted request/package and missing proof."""

import json
import unittest
from dataclasses import replace

import tests.support.fixtures_test_action_finalize_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_finalize_result_record import FinalizeWorkerResult
from literate_ai.adapters.action_package_execution import PackageWorkerResult
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)


class FinalizeResultTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.FinalizeWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        package = f.value.package_input
        self.value = replace(
            f.value,
            package_result_identity=record_identity(
                PackageWorkerResult(
                    record_identity(package.to_bytes()), f.package
                ).to_bytes()
            ),
        )
        self.raw = self.value.to_bytes()
        self.identity = record_identity(self.raw)
        evidence = StandardRootIntegrationEvidence(
            self.value.component_lock.identity,
            package.execution_plan.identity,
            self.value.project_plan.identity,
            package.artifact_graph,
            next(
                link
                for link in package.artifact_graph.link_plans
                if link.identity == package.plan.link_plan_identity
            ),
            package.plan,
            f.package,
            canonical_identity("test"),
            canonical_identity("execute"),
            canonical_identity("accept"),
        )
        # Deliberately synthetic references: this tests descriptor admission only.
        refs = tuple(
            sorted(
                (
                    BlobRef(identity.digest, 1)
                    for identity in (
                        evidence.identity,
                        evidence.root_generated_integration_test_identity,
                        evidence.packaged_execution_identity,
                        evidence.independent_acceptance_identity,
                    )
                ),
                key=lambda ref: ref.identity,
            )
        )
        self.result = FinalizeWorkerResult(self.identity, evidence, refs)

    def admit(self, result=None, raw=None, input_record=None):
        content = raw if raw is not None else (result or self.result).to_bytes()
        request = input_record if input_record is not None else self.raw
        return FinalizeWorkerResult.admit(
            content,
            record_identity(content),
            input_record=request,
            input_identity=record_identity(request),
            deadline=self.fixture.deadline,
        )

    def test_exact_result_roundtrip_is_only_descriptor_admission(self):
        self.assertEqual(self.admit(), self.result)

    def test_request_identity_substitution_refuses(self):
        with self.assertRaises(ActionWireError):
            self.admit(replace(self.result, input_identity=canonical_identity("other")))

    def test_package_receipt_substitution_refuses_even_with_updated_input_id(self):
        raw = replace(
            self.value, package_result_identity=canonical_identity("other-package")
        ).to_bytes()
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(self.result, input_identity=record_identity(raw)),
                input_record=raw,
            )

    def test_other_project_or_execution_cannot_bind_existing_package(self):
        for field in ("execution_plan_identity", "project_build_plan_identity"):
            evidence = replace(
                self.result.evidence, **{field: canonical_identity("foreign")}
            )
            # Keep the new evidence descriptor present so the request check matters.
            refs = tuple(
                sorted(
                    (
                        BlobRef(evidence.identity.digest, 1)
                        if ref.identity == self.result.evidence.identity.uri
                        else ref
                        for ref in self.result.evidence_records
                    ),
                    key=lambda ref: ref.identity,
                )
            )
            with self.subTest(field=field), self.assertRaises(ActionWireError):
                self.admit(
                    replace(self.result, evidence=evidence, evidence_records=refs)
                )

    def test_missing_duplicate_reordered_or_oversized_proof_refuses(self):
        refs = self.result.evidence_records
        for changed in (
            (),
            refs[:-1],
            (*refs, refs[-1]),
            tuple(reversed(refs)),
            (replace(refs[0], size=MAX_ACTION_RECORD_BYTES + 1), *refs[1:]),
        ):
            with self.subTest(refs=changed), self.assertRaises(ActionWireError):
                self.admit(replace(self.result, evidence_records=changed))

    def test_closed_canonical_envelope_refuses_unknown_fields_and_whitespace(self):
        raw = self.result.to_bytes()
        for changed in (
            raw + b"\n",
            canonical_json_bytes(json.loads(raw) | {"skip_verification": True}),
            b"[]",
        ):
            with self.assertRaises(ActionWireError):
                self.admit(raw=changed)
