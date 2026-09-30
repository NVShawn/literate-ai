"""Read and revalidate current persisted provider promotion without transitions."""

from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.authority import FileAuthorityProjectionStore
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)
from literate_ai.application.source_promotion import SourcePromotionService
from literate_ai.contracts import ComponentGenerationClosure, ContentIdentity
from literate_ai.source_to_specification.promotion_materialization import (
    VerifiedSourcePromotionEvidence,
)


def _reopen(
    root: Path,
    authority: LockedGenerationAuthority,
    closure: ComponentGenerationClosure,
    verifier: ContentIdentity,
    policy: ContentIdentity,
) -> VerifiedSourcePromotionEvidence:
    require_safe_directory(root)
    store = FileAuthorityProjectionStore(root)
    coordinate = authority.root_authoring.coordinate.uri
    projection = store.current(coordinate)
    service = SourcePromotionService()
    evidence = service.verify_evidence(root, projection)
    service.verify_qualified_locked(
        authority,
        evidence,
        current_generation_closure=closure,
        current_verifier_identity=verifier,
        current_policy_identity=policy,
    )
    if store.current(coordinate) != projection:
        raise ValueError("retained.provider.promotion-changed")
    require_safe_directory(root)
    return evidence


@dataclass(frozen=True, slots=True)
class CurrentQualifiedPromotion:
    """Reopened semantic evidence, with a guard for its current files and head.

    This guard is composed with the caller's lock, generation, verifier and policy
    guards. It reopens evidence under those expected identities; it cannot itself
    establish the currency of arguments supplied by the caller.
    """

    project_root: Path
    authority: LockedGenerationAuthority
    generation_closure: ComponentGenerationClosure
    verifier_identity: ContentIdentity
    policy_identity: ContentIdentity
    evidence: VerifiedSourcePromotionEvidence

    def require_unchanged(self) -> None:
        current = _reopen(
            self.project_root,
            self.authority,
            self.generation_closure,
            self.verifier_identity,
            self.policy_identity,
        )
        if current != self.evidence:
            raise ValueError("retained.provider.promotion-evidence-changed")


def read_current_qualified_promotion(
    project_root: Path,
    authority: LockedGenerationAuthority,
    *,
    current_generation_closure: ComponentGenerationClosure,
    current_verifier_identity: ContentIdentity,
    current_policy_identity: ContentIdentity,
) -> CurrentQualifiedPromotion:
    """Reopen the current provider head, audits and exact qualification result.

    Current arguments must be derived independently from project, generation and
    installed policy authority, never copied from the archive or historical head.
    Existing bounded evidence readers validate content-addressed records and actual
    audited inputs. No authority event, source workspace or evidence file is written.
    """
    if not isinstance(authority, LockedGenerationAuthority) or not isinstance(
        current_generation_closure, ComponentGenerationClosure
    ):
        raise TypeError("retained promotion requires current typed lock and closure")
    if any(
        not isinstance(identity, ContentIdentity)
        for identity in (current_verifier_identity, current_policy_identity)
    ):
        raise TypeError("retained promotion requires current verifier and policy")
    root = Path(project_root).absolute()
    if ".." in root.parts:
        raise ValueError("retained.provider.project-path-unsafe")
    evidence = _reopen(
        root,
        authority,
        current_generation_closure,
        current_verifier_identity,
        current_policy_identity,
    )
    result = CurrentQualifiedPromotion(
        root,
        authority,
        current_generation_closure,
        current_verifier_identity,
        current_policy_identity,
        evidence,
    )
    result.require_unchanged()
    return result
