"""Pure Component-authority transitions and drift evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.contracts.authority import (
    ComponentAuthorityProjection,
    ComponentAuthorityState,
    ComponentAuthorityTransition,
    ComponentGenerationClosure,
)
from literate_ai.contracts.identity import ContentIdentity


class AuthorityTransitionError(ValueError):
    """An authority transition attempted to skip or rewrite lifecycle history."""


_CURRENT_INPUT_UNAVAILABLE = object()


@dataclass(frozen=True, slots=True)
class AuthorityStatus:
    recorded_state: ComponentAuthorityState
    effective_state: ComponentAuthorityState
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "recorded_state": self.recorded_state.value,
            "effective_state": self.effective_state.value,
            "blockers": list(self.blockers),
        }


class ComponentAuthorityLifecycle:
    """Construct the only legal immutable projection sequence."""

    @staticmethod
    def inventory(
        *,
        component_coordinate: str,
        source_snapshot_identity: ContentIdentity,
        provenance_reference_identity: ContentIdentity,
        evidence_identities: tuple[ContentIdentity, ...],
    ) -> ComponentAuthorityProjection:
        return ComponentAuthorityProjection(
            component_coordinate=component_coordinate,
            component_revision_identity=None,
            state=ComponentAuthorityState.SOURCE_AUTHORITATIVE,
            transition=ComponentAuthorityTransition.SOURCE_INVENTORY,
            source_snapshot_identity=source_snapshot_identity,
            specification_set_identity=None,
            target_lock_identity=None,
            generation_closure=None,
            verifier_identity=None,
            policy_identity=None,
            evidence_identities=evidence_identities,
            provenance_reference_identity=provenance_reference_identity,
            prior_projection_identity=None,
        )

    @staticmethod
    def derive(
        prior: ComponentAuthorityProjection,
        *,
        component_revision_identity: ContentIdentity,
        specification_set_identity: ContentIdentity,
        evidence_identities: tuple[ContentIdentity, ...],
    ) -> ComponentAuthorityProjection:
        ComponentAuthorityLifecycle._require(
            prior, ComponentAuthorityState.SOURCE_AUTHORITATIVE
        )
        return ComponentAuthorityLifecycle._next(
            prior,
            state=ComponentAuthorityState.SPEC_ASSISTED,
            transition=ComponentAuthorityTransition.SPEC_DERIVATION,
            component_revision_identity=component_revision_identity,
            specification_set_identity=specification_set_identity,
            evidence_identities=evidence_identities,
        )

    @staticmethod
    def accept(
        prior: ComponentAuthorityProjection,
        *,
        evidence_identities: tuple[ContentIdentity, ...],
    ) -> ComponentAuthorityProjection:
        ComponentAuthorityLifecycle._require(
            prior, ComponentAuthorityState.SPEC_ASSISTED
        )
        return ComponentAuthorityLifecycle._next(
            prior,
            state=ComponentAuthorityState.DERIVED_SOURCE_RETAINED,
            transition=ComponentAuthorityTransition.HUMAN_ACCEPTANCE,
            evidence_identities=evidence_identities,
        )

    @staticmethod
    def qualify(
        prior: ComponentAuthorityProjection,
        *,
        target_lock_identity: ContentIdentity,
        generation_closure: ComponentGenerationClosure,
        verifier_identity: ContentIdentity,
        policy_identity: ContentIdentity,
        evidence_identities: tuple[ContentIdentity, ...],
    ) -> ComponentAuthorityProjection:
        ComponentAuthorityLifecycle._require(
            prior, ComponentAuthorityState.DERIVED_SOURCE_RETAINED
        )
        return ComponentAuthorityLifecycle._next(
            prior,
            state=ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE,
            transition=ComponentAuthorityTransition.REGENERATIVE_QUALIFICATION,
            target_lock_identity=target_lock_identity,
            generation_closure=generation_closure,
            verifier_identity=verifier_identity,
            policy_identity=policy_identity,
            evidence_identities=evidence_identities,
        )

    @staticmethod
    def invalidate(
        prior: ComponentAuthorityProjection,
        *,
        evidence_identities: tuple[ContentIdentity, ...],
    ) -> ComponentAuthorityProjection:
        ComponentAuthorityLifecycle._require(
            prior, ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE
        )
        return ComponentAuthorityProjection(
            component_coordinate=prior.component_coordinate,
            component_revision_identity=prior.component_revision_identity,
            state=ComponentAuthorityState.DERIVED_SOURCE_RETAINED,
            transition=ComponentAuthorityTransition.QUALIFICATION_INVALIDATION,
            source_snapshot_identity=prior.source_snapshot_identity,
            specification_set_identity=prior.specification_set_identity,
            target_lock_identity=None,
            generation_closure=None,
            verifier_identity=None,
            policy_identity=None,
            evidence_identities=evidence_identities,
            provenance_reference_identity=prior.provenance_reference_identity,
            prior_projection_identity=prior.identity,
        )

    @staticmethod
    def status(
        projection: ComponentAuthorityProjection,
        *,
        component_revision_identity: ContentIdentity | None = None,
        target_lock_identity: ContentIdentity | None | object = (
            _CURRENT_INPUT_UNAVAILABLE
        ),
        generation_closure: ComponentGenerationClosure | None | object = (
            _CURRENT_INPUT_UNAVAILABLE
        ),
        verifier_identity: ContentIdentity | None | object = _CURRENT_INPUT_UNAVAILABLE,
        policy_identity: ContentIdentity | None | object = _CURRENT_INPUT_UNAVAILABLE,
    ) -> AuthorityStatus:
        blockers: list[str] = []
        if projection.component_revision_identity != component_revision_identity:
            blockers.append("component-revision-changed")
        if projection.state is ComponentAuthorityState.DERIVED_SOURCE_RETAINED:
            blockers.append("regenerative-qualification-required")
        if (
            projection.state
            is ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE
        ):
            for reason, recorded, current in (
                (
                    "target-lock-changed",
                    projection.target_lock_identity,
                    target_lock_identity,
                ),
                (
                    "generation-closure-changed",
                    projection.generation_closure,
                    generation_closure,
                ),
                ("verifier-changed", projection.verifier_identity, verifier_identity),
                ("policy-changed", projection.policy_identity, policy_identity),
            ):
                if current is _CURRENT_INPUT_UNAVAILABLE:
                    blockers.append(reason.replace("-changed", "-not-evaluated"))
                elif recorded != current:
                    blockers.append(reason)
        effective = projection.state
        if (
            blockers
            and projection.state
            is ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE
        ):
            effective = ComponentAuthorityState.DERIVED_SOURCE_RETAINED
        elif "component-revision-changed" in blockers and projection.state in (
            ComponentAuthorityState.SPEC_ASSISTED,
            ComponentAuthorityState.DERIVED_SOURCE_RETAINED,
        ):
            effective = ComponentAuthorityState.SOURCE_AUTHORITATIVE
        return AuthorityStatus(projection.state, effective, tuple(blockers))

    @staticmethod
    def _require(
        prior: ComponentAuthorityProjection, expected: ComponentAuthorityState
    ) -> None:
        if prior.state is not expected:
            raise AuthorityTransitionError(
                f"expected {expected.value}; found {prior.state.value}"
            )

    @staticmethod
    def _next(
        prior: ComponentAuthorityProjection,
        *,
        state: ComponentAuthorityState,
        transition: ComponentAuthorityTransition,
        evidence_identities: tuple[ContentIdentity, ...],
        component_revision_identity: ContentIdentity | None = None,
        specification_set_identity: ContentIdentity | None = None,
        target_lock_identity: ContentIdentity | None = None,
        generation_closure: ComponentGenerationClosure | None = None,
        verifier_identity: ContentIdentity | None = None,
        policy_identity: ContentIdentity | None = None,
    ) -> ComponentAuthorityProjection:
        return ComponentAuthorityProjection(
            component_coordinate=prior.component_coordinate,
            component_revision_identity=(
                component_revision_identity or prior.component_revision_identity
            ),
            state=state,
            transition=transition,
            source_snapshot_identity=prior.source_snapshot_identity,
            specification_set_identity=(
                specification_set_identity or prior.specification_set_identity
            ),
            target_lock_identity=target_lock_identity,
            generation_closure=generation_closure,
            verifier_identity=verifier_identity,
            policy_identity=policy_identity,
            evidence_identities=evidence_identities,
            provenance_reference_identity=prior.provenance_reference_identity,
            prior_projection_identity=prior.identity,
        )


__all__ = ["AuthorityStatus", "AuthorityTransitionError", "ComponentAuthorityLifecycle"]
