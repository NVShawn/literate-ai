"""Bind regenerative source promotion to current locked generation authority."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts.authority import (
    ComponentAuthorityProjection,
    ComponentAuthorityState,
    ComponentGenerationClosure,
)
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.source_to_specification.inventory import SourceInventory
from literate_ai.source_to_specification.promotion_materialization import (
    GenerationInputAudit,
    SourceExclusionAssessment,
    SourcePromotionInput,
    SourcePromotionMaterializationResult,
    SourcePromotionMaterializer,
    VerifiedSourcePromotionEvidence,
    assess_source_exclusion,
    verify_source_promotion_evidence,
)


class LockedSourcePromotionError(ValueError):
    """Lifecycle promotion evidence does not match current locked authority."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class SourcePromotionService:
    """Public application boundary for source-free promotion operations."""

    def __init__(self, materializer: SourcePromotionMaterializer | None = None) -> None:
        self._materializer = materializer or SourcePromotionMaterializer()

    def materialize(
        self, inputs: Iterable[SourcePromotionInput], output_root: Path
    ) -> SourcePromotionMaterializationResult:
        return self._materializer.materialize(inputs, output_root)

    def audit(self, inputs: Iterable[SourcePromotionInput]) -> GenerationInputAudit:
        return self._materializer.audit(inputs)

    def assess_source_exclusion(
        self, audit: GenerationInputAudit, source_inventory: SourceInventory
    ) -> SourceExclusionAssessment:
        return assess_source_exclusion(audit, source_inventory)

    def verify_evidence(
        self, project_root: Path, projection: ComponentAuthorityProjection
    ) -> VerifiedSourcePromotionEvidence:
        return verify_source_promotion_evidence(project_root, projection)

    def qualify_locked(
        self,
        authority: LockedGenerationAuthority,
        evidence: VerifiedSourcePromotionEvidence,
    ) -> ComponentAuthorityProjection:
        return qualify_locked_source_promotion(authority, evidence)

    def verify_qualified_locked(
        self,
        authority: LockedGenerationAuthority,
        evidence: VerifiedSourcePromotionEvidence,
        *,
        current_generation_closure: ComponentGenerationClosure,
        current_verifier_identity: ContentIdentity,
        current_policy_identity: ContentIdentity,
    ) -> ComponentAuthorityProjection:
        return verify_qualified_locked_source_promotion(
            authority,
            evidence,
            current_generation_closure=current_generation_closure,
            current_verifier_identity=current_verifier_identity,
            current_policy_identity=current_policy_identity,
        )


def qualify_locked_source_promotion(
    authority: LockedGenerationAuthority,
    evidence: VerifiedSourcePromotionEvidence,
) -> ComponentAuthorityProjection:
    """Qualify one retained source projection under the exact current lock."""

    prior = _require_locked_promotion_evidence(
        authority,
        evidence,
        required_state=ComponentAuthorityState.DERIVED_SOURCE_RETAINED,
    )
    target_lock_identity = evidence.target_lock_identity
    qualified = ComponentAuthorityLifecycle.qualify(
        prior,
        target_lock_identity=target_lock_identity,
        generation_closure=evidence.generation_closure,
        verifier_identity=evidence.verifier_identity,
        policy_identity=evidence.policy_identity,
        evidence_identities=evidence.qualification_evidence_identities,
    )
    qualified.require_successor_of(prior)
    if qualified.target_lock_identity != authority.lock.identity:
        raise LockedSourcePromotionError(
            "promotion.lock_evidence_drift",
            "qualified authority did not preserve the exact current Component lock",
        )
    return qualified


def verify_qualified_locked_source_promotion(
    authority: LockedGenerationAuthority,
    evidence: VerifiedSourcePromotionEvidence,
    *,
    current_generation_closure: ComponentGenerationClosure,
    current_verifier_identity: ContentIdentity,
    current_policy_identity: ContentIdentity,
) -> ComponentAuthorityProjection:
    """Revalidate an existing qualified projection without creating a transition.

    The caller must reopen persisted evidence and independently resolve all current
    arguments. Copying historical fields into the current arguments is not a
    currency check. The returned projection does not authorize package consumption.
    """
    if not isinstance(current_generation_closure, ComponentGenerationClosure) or any(
        not isinstance(identity, ContentIdentity)
        for identity in (current_verifier_identity, current_policy_identity)
    ):
        raise TypeError(
            "qualified promotion requires independently resolved current authority"
        )
    projection = _require_locked_promotion_evidence(
        authority,
        evidence,
        required_state=ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE,
    )
    if (
        projection.generation_closure != evidence.generation_closure
        or projection.verifier_identity != evidence.verifier_identity
        or projection.policy_identity != evidence.policy_identity
    ):
        raise LockedSourcePromotionError(
            "promotion.qualification_evidence_drift",
            "reopened evidence differs from the current qualified projection",
        )
    status = ComponentAuthorityLifecycle.status(
        projection,
        component_revision_identity=authority.lock.root_revision,
        target_lock_identity=authority.lock.identity,
        generation_closure=current_generation_closure,
        verifier_identity=current_verifier_identity,
        policy_identity=current_policy_identity,
    )
    if status.blockers:
        raise LockedSourcePromotionError(
            "promotion.qualified_authority_stale",
            "qualified promotion differs from current generation, verifier "
            "or policy authority",
        )
    return projection


def _require_locked_promotion_evidence(
    authority: LockedGenerationAuthority,
    evidence: VerifiedSourcePromotionEvidence,
    *,
    required_state: ComponentAuthorityState,
) -> ComponentAuthorityProjection:
    if not isinstance(authority, LockedGenerationAuthority) or not isinstance(
        authority.lock, ComponentLock
    ):
        raise TypeError("source promotion requires current locked generation authority")
    if not isinstance(evidence, VerifiedSourcePromotionEvidence):
        raise TypeError("source promotion requires verified lifecycle evidence")
    from literate_ai.source_to_specification.qualification_lifecycle import (
        QualificationLifecycleResult,
    )

    qualification = evidence.qualification_lifecycle_result
    if not isinstance(qualification, QualificationLifecycleResult):
        raise LockedSourcePromotionError(
            "promotion.qualification_contract_legacy",
            "legacy regenerative qualification evidence cannot authorize a new "
            "fungible transition; lifecycle-backed qualification is required",
        )

    prior = evidence.authority_projection
    if prior is None:
        raise LockedSourcePromotionError(
            "promotion.lifecycle_evidence_missing",
            "qualification requires the current v2 Component authority projection",
        )
    if prior.state is not required_state:
        raise LockedSourcePromotionError(
            "promotion.lifecycle_state_invalid",
            f"source promotion requires {required_state.value} authority",
        )

    target_lock_identity = evidence.target_lock_identity
    if target_lock_identity is None:
        raise LockedSourcePromotionError(
            "promotion.lock_evidence_missing",
            "qualification evidence must name the exact current Component lock",
        )
    if target_lock_identity != authority.lock.identity:
        raise LockedSourcePromotionError(
            "promotion.lock_evidence_stale",
            "qualification evidence names a stale or different Component lock",
        )

    root_node = next(
        node
        for node in authority.lock.nodes
        if node.revision.identity == authority.lock.root_revision
    )
    if (
        prior.component_coordinate != authority.root_authoring.coordinate.uri
        or prior.component_revision_identity != root_node.revision.identity
        or prior.specification_set_identity
        != root_node.revision.specification_set_identity
    ):
        raise LockedSourcePromotionError(
            "promotion.lock_evidence_drift",
            "promotion authority facts differ from the current locked root revision",
        )

    if (
        evidence.generation_closure is None
        or evidence.verifier_identity is None
        or evidence.policy_identity is None
    ):
        raise LockedSourcePromotionError(
            "promotion.qualification_evidence_missing",
            "qualification requires exact closure, verifier, and policy evidence",
        )
    if (
        evidence.generation_closure.qualification_evidence_identity
        != qualification.identity
        or evidence.verifier_identity != qualification.case_map.verifier_identity
        or evidence.policy_identity != qualification.runs[0].lifecycle_policy_identity
    ):
        raise LockedSourcePromotionError(
            "promotion.qualification_evidence_drift",
            "lifecycle qualification result differs from its admitted closure",
        )
    if any(
        run.target_profile_identity != authority.lock.target_profile_identity
        or run.component_lock_identity != authority.lock.identity
        or run.specification_set_identity != prior.specification_set_identity
        or run.source_snapshot_identity != prior.source_snapshot_identity
        or run.generation_input_audit_identity
        != evidence.generation_closure.promotion_input_audit_identity
        or run.promotion_tree_identity
        != evidence.generation_closure.promotion_tree_identity
        for run in qualification.runs
    ):
        raise LockedSourcePromotionError(
            "promotion.qualification_evidence_drift",
            "lifecycle qualification runs differ from retained source authority",
        )

    return prior


__all__ = [
    "LockedSourcePromotionError",
    "SourcePromotionService",
    "qualify_locked_source_promotion",
    "verify_qualified_locked_source_promotion",
]
