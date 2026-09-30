"""Evidence-bound final cutover contracts for downstream framework adoption."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from literate_ai.contracts import ContentIdentity, LifecycleSeam, canonical_identity


class ObservationDisposition(StrEnum):
    """How the production observation gate was disposed."""

    COMPLETED = "completed"
    USER_DIRECTED_WITHOUT_ELAPSED_WINDOW = "user-directed-without-elapsed-window"


@dataclass(frozen=True, slots=True)
class ObservationDecision:
    """Evidence or explicit authority for the production-observation gate.

    A user-directed cutover is deliberately not represented as completed observation.
    It is a visible risk acceptance that permits a cutover while preserving the fact
    that no elapsed production window was proved.
    """

    disposition: ObservationDisposition
    decided_by: str
    reason: str
    evidence_identity: ContentIdentity | None = None
    directive_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.decided_by.strip() or not self.reason.strip():
            raise ValueError("observation decision requires an actor and reason")
        if self.disposition is ObservationDisposition.COMPLETED:
            if self.evidence_identity is None or self.directive_reference is not None:
                raise ValueError(
                    "completed observation requires evidence and no directive override"
                )
        elif (
            self.disposition
            is ObservationDisposition.USER_DIRECTED_WITHOUT_ELAPSED_WINDOW
        ):
            if self.evidence_identity is not None or not (
                self.directive_reference and self.directive_reference.strip()
            ):
                raise ValueError(
                    "user-directed cutover requires an exact directive reference and "
                    "must not claim observation evidence"
                )
        else:  # pragma: no cover - defensive against non-enum callers
            raise ValueError("unsupported observation disposition")

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "evidence_identity": (
                self.evidence_identity.to_dict()
                if self.evidence_identity is not None
                else None
            ),
            "directive_reference": self.directive_reference,
        }


@dataclass(frozen=True, slots=True)
class RollbackRehearsal:
    """Immutable proof that retained state can recover without destructive rewrite."""

    baseline_state_identity: ContentIdentity
    migrated_copy_identity: ContentIdentity
    evidence_identity: ContentIdentity
    prior_release_restore_verified: bool
    compatibility_reader_verified: bool
    baseline_unchanged: bool

    @property
    def passed(self) -> bool:
        return (
            self.prior_release_restore_verified
            and self.compatibility_reader_verified
            and self.baseline_unchanged
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline_state_identity": self.baseline_state_identity.to_dict(),
            "migrated_copy_identity": self.migrated_copy_identity.to_dict(),
            "evidence_identity": self.evidence_identity.to_dict(),
            "prior_release_restore_verified": self.prior_release_restore_verified,
            "compatibility_reader_verified": self.compatibility_reader_verified,
            "baseline_unchanged": self.baseline_unchanged,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class SeamCutover:
    """Final ownership and evidence for one provider-neutral lifecycle seam."""

    seam: LifecycleSeam
    comparison_evidence_identity: ContentIdentity
    framework_write_authority: bool = True
    downstream_legacy_write_removed: bool = True
    compatibility_read_retained: bool = True

    @property
    def passed(self) -> bool:
        return (
            self.framework_write_authority
            and self.downstream_legacy_write_removed
            and self.compatibility_read_retained
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "seam": self.seam.value,
            "comparison_evidence_identity": self.comparison_evidence_identity.to_dict(),
            "framework_write_authority": self.framework_write_authority,
            "downstream_legacy_write_removed": self.downstream_legacy_write_removed,
            "compatibility_read_retained": self.compatibility_read_retained,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class FinalCutoverManifest:
    """Machine-verifiable decision record for retiring downstream duplication."""

    framework_release_identity: ContentIdentity
    compatible_release_identities: tuple[ContentIdentity, ...]
    seams: tuple[SeamCutover, ...]
    compatibility_reader_identity: ContentIdentity
    compatibility_reader_support_term: str
    dependency_audit_identity: ContentIdentity
    duplicate_general_lifecycle_implementation_removed: bool
    rollback: RollbackRehearsal
    observation: ObservationDecision

    def __post_init__(self) -> None:
        if len(self.compatible_release_identities) < 2:
            raise ValueError(
                "final cutover requires at least two compatibility releases"
            )
        release_uris = tuple(item.uri for item in self.compatible_release_identities)
        if len(set(release_uris)) != len(release_uris):
            raise ValueError("compatibility release identities must be distinct")
        expected = tuple(LifecycleSeam)
        actual = tuple(item.seam for item in self.seams)
        if actual != expected:
            raise ValueError(
                "final cutover must cover every lifecycle seam in canonical order"
            )
        if not self.compatibility_reader_support_term.strip():
            raise ValueError("compatibility reader support term must be explicit")

    @property
    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        blockers.extend(
            f"seam:{item.seam.value}" for item in self.seams if not item.passed
        )
        if not self.duplicate_general_lifecycle_implementation_removed:
            blockers.append("duplicate-general-lifecycle-implementation")
        if not self.rollback.passed:
            blockers.append("rollback-rehearsal")
        return tuple(blockers)

    @property
    def warnings(self) -> tuple[str, ...]:
        if (
            self.observation.disposition
            is ObservationDisposition.USER_DIRECTED_WITHOUT_ELAPSED_WINDOW
        ):
            return ("production-observation-window-not-completed",)
        return ()

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai-final-cutover@1",
            "framework_release_identity": self.framework_release_identity.to_dict(),
            "compatible_release_identities": [
                item.to_dict() for item in self.compatible_release_identities
            ],
            "seams": [item.to_dict() for item in self.seams],
            "compatibility_reader_identity": (
                self.compatibility_reader_identity.to_dict()
            ),
            "compatibility_reader_support_term": self.compatibility_reader_support_term,
            "dependency_audit_identity": self.dependency_audit_identity.to_dict(),
            "duplicate_general_lifecycle_implementation_removed": (
                self.duplicate_general_lifecycle_implementation_removed
            ),
            "rollback": self.rollback.to_dict(),
            "observation": self.observation.to_dict(),
            "ready": self.ready,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


__all__ = [
    "FinalCutoverManifest",
    "ObservationDecision",
    "ObservationDisposition",
    "RollbackRehearsal",
    "SeamCutover",
]
