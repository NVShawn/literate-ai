"""Security policy gates over exact identities."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.security import (
    AuthorizationError,
    BuildRequest,
    FindingSeverity,
    OriginAttestation,
    SecurityFinding,
    SecurityPolicy,
    SecurityProfile,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
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


if __name__ == "__main__":
    unittest.main()
