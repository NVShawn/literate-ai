"""Security policy gates over exact identities."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.security import (
    AuthorizationError,
    BuildRequest,
    FindingSeverity,
    ObservationRequest,
    OriginAttestation,
    SecurityFinding,
    SecurityPolicy,
    SecurityProfile,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
NOW = datetime(2026, 8, 2, tzinfo=UTC)


class SecurityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = SecurityPolicy(policy_digest=DIGEST_D)
        self.attestation = OriginAttestation(
            source_digest=DIGEST_A,
            signer="release@example.test",
            trust_root="test-root",
            signature_identity="sigstore:test",
            verified=True,
        )

    def classification(self, severity: FindingSeverity = FindingSeverity.LOW):
        return self.policy.classify(
            effective_revision_digest=DIGEST_B,
            attestations=[self.attestation],
            findings=[
                SecurityFinding(
                    finding_id="finding:test",
                    source_digest=DIGEST_A,
                    category="test",
                    severity=severity,
                    scanner_identity="scanner:test@1",
                    message="fixture",
                )
            ],
        )

    def test_signature_is_required_but_does_not_override_blocking_finding(self) -> None:
        classification = self.classification(FindingSeverity.CRITICAL)
        self.assertEqual(classification.profile, SecurityProfile.BLOCKED)

    def test_revoked_origin_fails_before_classification(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "origin_not_verified"):
            self.policy.classify(
                effective_revision_digest=DIGEST_B,
                attestations=[replace(self.attestation, revoked=True)],
                findings=[],
            )

    def test_normal_build_authorization_is_exact_and_expiring(self) -> None:
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=DIGEST_A,
            builder_id="builder:python@1",
            toolchain_digest=DIGEST_A,
            sandbox_profile="constrained",
            requested_privileges=("compiler",),
            allowed_outputs=("wheel",),
        )
        authorization = self.policy.authorize_build(
            self.classification(),
            request,
            actor="maintainer",
            reason="test build",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        authorization.require_valid(request, now=NOW + timedelta(minutes=1))
        with self.assertRaisesRegex(AuthorizationError, "expired"):
            authorization.require_valid(request, now=NOW + timedelta(minutes=11))
        with self.assertRaisesRegex(AuthorizationError, "not_yet_valid"):
            authorization.require_valid(request, now=NOW - timedelta(seconds=1))

        reissued = self.policy.authorize_build(
            self.classification(),
            request,
            actor="maintainer",
            reason="test build",
            issued_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=10),
        )
        self.assertNotEqual(authorization.authorization_id, reissued.authorization_id)

    def test_build_authorization_requires_the_classified_source(self) -> None:
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=DIGEST_C,
            builder_id="builder:python@1",
            toolchain_digest=DIGEST_A,
            sandbox_profile="constrained",
            requested_privileges=("compiler",),
            allowed_outputs=("wheel",),
        )

        with self.assertRaisesRegex(AuthorizationError, "source_mismatch"):
            self.policy.authorize_build(
                self.classification(),
                request,
                actor="maintainer",
                reason="wrong source",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )

    def test_yolo_requires_acknowledgement_but_grants_only_requested_privileges(
        self,
    ) -> None:
        requested = ("compiler", "sandbox-escape")
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=DIGEST_A,
            builder_id="builder:any@1",
            toolchain_digest=DIGEST_A,
            sandbox_profile="maximum-privilege",
            requested_privileges=requested,
            allowed_outputs=("artifact",),
        )
        blocked = self.classification(FindingSeverity.CRITICAL)
        with self.assertRaisesRegex(AuthorizationError, "build_blocked"):
            self.policy.authorize_build(
                blocked,
                request,
                actor="maintainer",
                reason="explicit risk acceptance",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
        authorization = self.policy.authorize_build(
            blocked,
            request,
            actor="maintainer",
            reason="explicit risk acceptance",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        self.assertEqual(authorization.profile, SecurityProfile.YOLO)
        self.assertEqual(authorization.privileges, tuple(sorted(requested)))
        self.assertIn("MAXIMUM PRIVILEGE", authorization.warning or "")

    def test_constrained_profile_cannot_launder_privileged_capabilities(self) -> None:
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=DIGEST_A,
            builder_id="builder:any@1",
            toolchain_digest=DIGEST_A,
            sandbox_profile="constrained",
            requested_privileges=("compiler", "secrets"),
            allowed_outputs=("artifact",),
        )
        with self.assertRaisesRegex(AuthorizationError, "privilege_not_permitted"):
            self.policy.authorize_build(
                self.classification(),
                request,
                actor="maintainer",
                reason="must not escalate",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )

        explicitly_escalated = self.policy.authorize_build(
            self.classification(),
            request,
            actor="maintainer",
            reason="acknowledged unsandboxed host build",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        self.assertEqual(explicitly_escalated.profile, SecurityProfile.YOLO)
        self.assertEqual(
            explicitly_escalated.privileges,
            tuple(sorted(request.requested_privileges)),
        )
        self.assertIn("MAXIMUM PRIVILEGE", explicitly_escalated.warning or "")

        privileged = replace(
            self.classification(),
            profile=SecurityProfile.PRIVILEGED_REVIEW,
            permitted_privileges=("compiler", "secrets"),
        )
        authorization = self.policy.authorize_build(
            privileged,
            request,
            actor="maintainer",
            reason="explicit privileged review",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        self.assertEqual(authorization.profile, SecurityProfile.PRIVILEGED_REVIEW)

    def test_observation_authorization_is_separate_and_exact(self) -> None:
        request = ObservationRequest(
            effective_revision_digest=DIGEST_B,
            source_digests=(DIGEST_A,),
            runner_id="runner:test@1",
            harness_digest=DIGEST_C,
            sandbox_profile="observation",
            requested_privileges=("processes",),
            allowed_outputs=("trace",),
        )
        authorization = self.policy.authorize_observation(
            self.classification(),
            request,
            actor="reviewer",
            reason="collect state transitions",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        authorization.require_valid(request, now=NOW + timedelta(minutes=1))

    def test_security_contracts_reject_noncanonical_and_duplicate_identity_data(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "lowercase sha256"):
            replace(self.attestation, source_digest="sha256:" + "A" * 64)
        with self.assertRaisesRegex(ValueError, "lowercase sha256"):
            replace(self.attestation, source_digest="sha256:" + "z" * 64)
        with self.assertRaisesRegex(ValueError, "unique"):
            BuildRequest(
                DIGEST_B,
                DIGEST_A,
                "builder:test@1",
                DIGEST_C,
                "constrained",
                ("compiler", "compiler"),
                ("artifact",),
            )
        with self.assertRaisesRegex(ValueError, "allowed_outputs"):
            BuildRequest(
                DIGEST_B,
                DIGEST_A,
                "builder:test@1",
                DIGEST_C,
                "constrained",
                (),
                (),
            )
        with self.assertRaisesRegex(ValueError, "finding_ids"):
            replace(
                self.classification(),
                finding_ids=("finding:test", "finding:test"),
            )
        finding_document = SecurityFinding(
            "finding:strict-wire-type",
            DIGEST_A,
            "test",
            FindingSeverity.LOW,
            "scanner:test@1",
            "strict integer wire type",
        ).to_dict()
        finding_document["severity"] = "1"
        with self.assertRaisesRegex(ValueError, "severity must be an integer"):
            SecurityFinding.from_dict(finding_document)
        classification_document = self.classification().to_dict()
        classification_document["maximum_severity"] = True
        with self.assertRaisesRegex(ValueError, "maximum_severity must be an integer"):
            type(self.classification()).from_dict(classification_document)

    def test_observation_yolo_requires_acknowledgement_and_binds_warning(self) -> None:
        request = ObservationRequest(
            effective_revision_digest=DIGEST_B,
            source_digests=(DIGEST_A,),
            runner_id="runner:host@1",
            harness_digest=DIGEST_C,
            sandbox_profile="host",
            requested_privileges=("host-filesystem", "processes"),
            allowed_outputs=("trace",),
        )
        classification = replace(
            self.classification(),
            profile=SecurityProfile.YOLO,
            permitted_privileges=("host-filesystem", "processes"),
        )
        with self.assertRaisesRegex(AuthorizationError, "yolo_not_acknowledged"):
            self.policy.authorize_observation(
                classification,
                request,
                actor="reviewer",
                reason="explicit host observation",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
        authorization = self.policy.authorize_observation(
            classification,
            request,
            actor="reviewer",
            reason="explicit host observation",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        self.assertEqual(authorization.profile, SecurityProfile.YOLO)
        self.assertIn("MAXIMUM PRIVILEGE", authorization.warning or "")
        self.assertEqual(
            authorization.effective_revision_digest,
            request.effective_revision_digest,
        )


if __name__ == "__main__":
    unittest.main()
