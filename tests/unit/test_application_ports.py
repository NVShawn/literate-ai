"""Executable contracts for the application ports that adapters actually implement."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import literate_ai.application as application
import literate_ai.ports as ports
from literate_ai.application import BuildAuthorizer
from literate_ai.contracts import canonical_identity
from literate_ai.ports import (
    AUTHORIZED_EXECUTION_PROFILE,
    NON_EXECUTING_EXACT_TREE_PROFILE,
    PortContractError,
    require_acceptance_result,
    require_build_request_document,
    require_build_result,
    require_classification_result,
    require_generated_test_result,
)
from literate_ai.security import (
    BuildRequest,
    ObservationExecutionAuthorization,
    ObservationRequest,
    SecurityProfile,
)
from tests.support.fixtures_test_application_generation import identity


class ApplicationPortTests(unittest.TestCase):
    def test_build_request_port_uses_one_fail_closed_v2_identity(self) -> None:
        request = {
            "effective_revision_digest": identity("revision").uri,
            "source_bundle_digest": identity("source").uri,
            "builder_id": "builder:test@1",
            "toolchain_digest": identity("toolchain").uri,
            "sandbox_profile": "constrained",
            "requested_privileges": ["compiler"],
            "allowed_outputs": ["artifact"],
        }

        current = require_build_request_document(request)

        self.assertEqual(current, BuildRequest.from_dict(request).to_dict())
        for unsupported in (None, "urn:literate-ai:schema:v3:build-request"):
            with self.subTest(schema=unsupported):
                with self.assertRaises(PortContractError) as raised:
                    require_build_request_document({**request, "schema": unsupported})
                self.assertEqual(
                    raised.exception.code,
                    "ports.build-request.schema-unsupported",
                )

    def test_authorized_acceptance_requires_typed_bound_execution_evidence(
        self,
    ) -> None:
        revision = identity("revision").uri
        source = identity("source").uri
        classification = identity("classification").uri
        request = ObservationRequest(
            revision,
            (source,),
            "runner:authorized-host@1",
            identity("harness").uri,
            "host-process",
            ("processes",),
            ("stdout",),
        )
        authorization = ObservationExecutionAuthorization(
            "observe-auth:fixture",
            classification,
            canonical_identity(request.to_dict()).uri,
            revision,
            "tester",
            "execute exact acceptance case",
            SecurityProfile.CONSTRAINED,
            ("processes",),
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5),
        )

        result = require_acceptance_result(
            {
                "passed": True,
                "runner_id": "acceptance:fixture@1",
                "execution_profile": AUTHORIZED_EXECUTION_PROFILE,
                "classification_digest": classification,
                "effective_revision_digest": revision,
                "source_bundle_digest": source,
                "artifact_digest": identity("artifact").uri,
                "dependency_resolution_identity": identity("resolution").uri,
                "verified_tree_identity": identity("tree").uri,
                "test_suite_identity": identity("acceptance-suite").uri,
                "total": 1,
                "passed_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "case_results": [
                    {
                        "case_id": "primary",
                        "passed": True,
                        "observation_request": request.to_dict(),
                        "execution_authorization": authorization.to_dict(),
                    }
                ],
            },
            runner_id="acceptance:fixture@1",
            classification_digest=classification,
            effective_revision_digest=revision,
            source_bundle_digest=source,
            artifact_digest=identity("artifact").uri,
            dependency_resolution_identity=identity("resolution").uri,
            tree_identity=identity("tree").uri,
            test_suite_identity=identity("acceptance-suite").uri,
            expected_execution_profile=AUTHORIZED_EXECUTION_PROFILE,
        )

        self.assertEqual(
            result["case_results"][0]["observation_request"], request.to_dict()
        )

    def test_lifecycle_outputs_must_bind_the_exact_authority_inputs(self) -> None:
        expected_revision = identity("revision").uri
        expected_source = identity("source").uri
        with self.assertRaises(PortContractError) as classification_error:
            require_classification_result(
                {
                    "classification_digest": identity("classification").uri,
                    "effective_revision_digest": expected_revision,
                    "source_digests": [identity("other-source").uri],
                    "profile": "constrained",
                },
                effective_revision_digest=expected_revision,
                source_bundle_digest=expected_source,
            )
        self.assertEqual(
            classification_error.exception.code,
            "ports.classification.source-mismatch",
        )

        with self.assertRaises(PortContractError) as build_error:
            require_build_result(
                {
                    "artifact_digest": identity("artifact").uri,
                    "source_bundle_digest": expected_source,
                    "authorization_id": "authorization:other",
                    "compiled_files": ["main.pyc"],
                },
                source_bundle_digest=expected_source,
                authorization_id="authorization:expected",
            )
        self.assertEqual(
            build_error.exception.code,
            "ports.build.authorization-mismatch",
        )

        with self.assertRaises(PortContractError) as generated_test_error:
            require_generated_test_result(
                {
                    "passed": True,
                    "runner_id": "tester:fixture@1",
                    "execution_profile": NON_EXECUTING_EXACT_TREE_PROFILE,
                    "profile_reason": "identity-only fixture",
                    "classification_digest": identity("classification").uri,
                    "effective_revision_digest": expected_revision,
                    "source_bundle_digest": expected_source,
                    "artifact_digest": identity("other-artifact").uri,
                    "dependency_resolution_identity": identity("resolution").uri,
                    "verified_tree_identity": identity("tree").uri,
                    "test_suite_identity": identity("suite").uri,
                    "total": 1,
                    "passed_count": 1,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "case_results": [{"case_id": "example", "passed": True}],
                },
                runner_id="tester:fixture@1",
                classification_digest=identity("classification").uri,
                effective_revision_digest=expected_revision,
                source_bundle_digest=expected_source,
                artifact_digest=identity("artifact").uri,
                dependency_resolution_identity=identity("resolution").uri,
                tree_identity=identity("tree").uri,
                test_suite_identity=identity("suite").uri,
                expected_case_ids=("example",),
            )
        self.assertEqual(
            generated_test_error.exception.code,
            "ports.generated-tests.artifact-mismatch",
        )

        with self.assertRaises(PortContractError) as missing_evidence:
            require_acceptance_result(
                {
                    "passed": True,
                    "runner_id": "acceptance:fixture@1",
                    "execution_profile": AUTHORIZED_EXECUTION_PROFILE,
                    "classification_digest": identity("classification").uri,
                    "effective_revision_digest": expected_revision,
                    "source_bundle_digest": expected_source,
                    "artifact_digest": identity("artifact").uri,
                    "dependency_resolution_identity": identity("resolution").uri,
                    "verified_tree_identity": identity("tree").uri,
                    "test_suite_identity": identity("acceptance-suite").uri,
                    "total": 1,
                    "passed_count": 1,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "case_results": [{"case_id": "primary", "passed": True}],
                },
                runner_id="acceptance:fixture@1",
                classification_digest=identity("classification").uri,
                effective_revision_digest=expected_revision,
                source_bundle_digest=expected_source,
                artifact_digest=identity("artifact").uri,
                dependency_resolution_identity=identity("resolution").uri,
                tree_identity=identity("tree").uri,
                test_suite_identity=identity("acceptance-suite").uri,
                expected_execution_profile=AUTHORIZED_EXECUTION_PROFILE,
            )
        self.assertEqual(
            missing_evidence.exception.code,
            "ports.acceptance.execution-evidence-invalid",
        )

    def test_speculative_protocol_catalog_is_not_public_api(self) -> None:
        for name in (
            "ArtifactStore",
            "CatalogProvider",
            "IntelligenceProvider",
            "OriginVerifier",
            "Publisher",
            "ReferenceStore",
            "ResolverPolicy",
            "SettingsProvider",
            "SandboxRunner",
            "SourceProvider",
            "SpecificationProvider",
        ):
            self.assertNotIn(name, ports.__all__)
            self.assertFalse(hasattr(ports, name))
        self.assertNotIn("RouteSelector", application.__all__)
        self.assertFalse(hasattr(application, "RouteSelector"))
        self.assertIs(BuildAuthorizer, ports.BuildAuthorizer)


if __name__ == "__main__":
    unittest.main()
