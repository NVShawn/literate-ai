"""Verifier-owned run requirements and checked graph custody."""

from __future__ import annotations

import re
from dataclasses import dataclass

from literate_ai.contracts._validation import int_value
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from .checks import CheckedEvidenceAssertion, EvidenceTrustError
from .records import (
    DSSE_MEDIA_TYPE,
    DerivationRun,
    EvidenceArtifact,
    EvidenceMatrix,
    PlatformRun,
)
from .storage import DEFAULT_EVIDENCE_READ_LIMITS, EvidenceReadLimits, ResolvedEvidence
from .trust import EvidenceRevocations, RunEvidenceExpectation

_ROLE = re.compile(
    r"(?:subject|journal|environment|(?:input|check)/[a-zA-Z0-9][a-zA-Z0-9._-]{0,127})\Z"
)


@dataclass(frozen=True, slots=True)
class EvidenceRunRequirement:
    """Exact planned envelope, child edges and named leaf artifacts for one run.

    Artifact roles are subject, journal, environment, input/NAME and check/NAME.
    Child names are matrix cell names or the platform's single derivation edge.
    Construct this from independent execution authority, never the claimed run.
    """

    envelope: BlobRef
    expectation: RunEvidenceExpectation
    children: tuple[EvidenceArtifact, ...]
    artifacts: tuple[tuple[str, BlobRef], ...]

    def __post_init__(self):
        EvidenceArtifact("envelope", self.envelope)
        if self.envelope.media_type != DSSE_MEDIA_TYPE or not isinstance(
            self.expectation, RunEvidenceExpectation
        ):
            raise EvidenceTrustError("evidence.graph.requirement-invalid")
        if (
            not isinstance(self.children, tuple)
            or len(self.children) > 1024
            or any(not isinstance(item, EvidenceArtifact) for item in self.children)
            or not isinstance(self.artifacts, tuple)
            or not 1 <= len(self.artifacts) <= 1026
        ):
            raise EvidenceTrustError("evidence.graph.requirement-invalid")
        names = tuple(item.name for item in self.children)
        if names != tuple(sorted(set(names))) or any(
            item.blob.media_type != DSSE_MEDIA_TYPE for item in self.children
        ):
            raise EvidenceTrustError("evidence.graph.requirement-invalid")
        roles = []
        for item in self.artifacts:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not _ROLE.fullmatch(item[0])
            ):
                raise EvidenceTrustError("evidence.graph.requirement-invalid")
            EvidenceArtifact("artifact", item[1])
            roles.append(item[0])
        if roles != sorted(set(roles)):
            raise EvidenceTrustError("evidence.graph.requirement-invalid")
        artifacts = dict(self.artifacts)
        if artifacts.get("subject") != self.expectation.subject:
            raise EvidenceTrustError("evidence.graph.subject-mismatch")
        kind = self.expectation.predicate_type
        if kind == EvidenceMatrix.SCHEMA:
            valid = names == self.expectation.required_cells and roles == ["subject"]
        elif kind == PlatformRun.SCHEMA:
            valid = (
                names == ("derivation",)
                and "environment" in artifacts
                and any(role.startswith("check/") for role in roles)
                and all(
                    role in ("subject", "environment") or role.startswith("check/")
                    for role in roles
                )
            )
        else:
            valid = (
                kind == DerivationRun.SCHEMA
                and not names
                and "journal" in artifacts
                and any(role.startswith("input/") for role in roles)
                and all(
                    role in ("subject", "journal") or role.startswith("input/")
                    for role in roles
                )
            )
        if not valid:
            raise EvidenceTrustError("evidence.graph.requirement-invalid")

    def to_dict(self) -> dict:
        """Canonical fields embedded in independent verification plans."""
        return {
            "envelope": self.envelope.to_dict(),
            "expectation": self.expectation.to_dict(),
            "children": [item.to_dict() for item in self.children],
            "artifacts": [
                [role, reference.to_dict()] for role, reference in self.artifacts
            ],
        }


@dataclass(frozen=True, slots=True)
class EvidenceVerificationState:
    """Trusted provider's current clock and revocation snapshot."""

    now: int
    revocations: EvidenceRevocations

    def __post_init__(self):
        int_value(self.now, "evidence.graph.now")
        if not isinstance(self.revocations, EvidenceRevocations):
            raise EvidenceTrustError("evidence.graph.state-invalid")


@dataclass(frozen=True, slots=True)
class EvidenceGraphLimits:
    reads: EvidenceReadLimits = DEFAULT_EVIDENCE_READ_LIMITS
    maximum_signature_checks: int = 1_000_000

    def __post_init__(self):
        if not isinstance(self.reads, EvidenceReadLimits):
            raise EvidenceTrustError("evidence.graph.limits-invalid")
        int_value(
            self.maximum_signature_checks,
            "evidence.graph.signature_checks",
            minimum=1,
            maximum=2**31,
        )


@dataclass(frozen=True, slots=True)
class CheckedRunEvidenceGraph:
    """Retained authenticated run graph, without retention or receipt admission.

    Results cannot be deserialized into proof or grant execution authority. Store
    availability and authorized retention claims require their own admission step.
    """

    root: BlobRef
    plan_identity: ContentIdentity
    policy_identity: ContentIdentity
    initial_state: EvidenceVerificationState
    final_state: EvidenceVerificationState
    runs: tuple[tuple[BlobRef, CheckedEvidenceAssertion], ...]
    objects: tuple[ResolvedEvidence, ...]

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "root": self.root.to_dict(),
                "plan": self.plan_identity.to_dict(),
                "policy": self.policy_identity.to_dict(),
                "initial_revocations": (
                    self.initial_state.revocations.identity.to_dict()
                ),
                "final_revocations": self.final_state.revocations.identity.to_dict(),
                "started_at": self.initial_state.now,
                "checked_at": self.final_state.now,
                "runs": [
                    [
                        ref.to_dict(),
                        checked.expectation_identity.to_dict(),
                        list(checked.qualified_signer_key_identities),
                    ]
                    for ref, checked in self.runs
                ],
                "objects": [
                    [item.reference.to_dict(), item.locator.to_dict()]
                    for item in self.objects
                ],
            }
        )
