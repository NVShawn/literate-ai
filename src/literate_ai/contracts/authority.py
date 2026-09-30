"""Immutable authority projections for a Component's source/spec lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    list_value,
    string_value,
    unique,
)
from .identity import ContentIdentity, contract_identity

COMPONENT_AUTHORITY_PROJECTION_SCHEMA = (
    "urn:literate-ai:schema:v2:component-authority-projection"
)
_COMPONENT_COORDINATE = re.compile(
    r"component://[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*"
)


class ComponentAuthorityState(StrEnum):
    SOURCE_AUTHORITATIVE = "source-authoritative"
    SPEC_ASSISTED = "spec-assisted"
    DERIVED_SOURCE_RETAINED = "derived-source-retained"
    REGENERATIVELY_QUALIFIED_FUNGIBLE = "regeneratively-qualified-fungible"


class ComponentAuthorityTransition(StrEnum):
    SOURCE_INVENTORY = "source-inventory"
    SPEC_DERIVATION = "spec-derivation"
    HUMAN_ACCEPTANCE = "human-acceptance"
    REGENERATIVE_QUALIFICATION = "regenerative-qualification"
    QUALIFICATION_INVALIDATION = "qualification-invalidation"


@dataclass(frozen=True, slots=True)
class ComponentGenerationClosure:
    flavor_set_identity: ContentIdentity
    skill_set_identity: ContentIdentity
    workflow_identity: ContentIdentity
    routing_policy_identity: ContentIdentity
    promotion_input_audit_identity: ContentIdentity
    promotion_tree_identity: ContentIdentity
    qualification_evidence_identity: ContentIdentity

    def __post_init__(self) -> None:
        for field in (
            "flavor_set_identity",
            "skill_set_identity",
            "workflow_identity",
            "routing_policy_identity",
            "promotion_input_audit_identity",
            "promotion_tree_identity",
            "qualification_evidence_identity",
        ):
            if not isinstance(getattr(self, field), ContentIdentity):
                fail(f"ComponentGenerationClosure.{field}", "must be a ContentIdentity")

    def to_dict(self) -> dict[str, str]:
        return {
            "flavor_set_identity": self.flavor_set_identity.uri,
            "skill_set_identity": self.skill_set_identity.uri,
            "workflow_identity": self.workflow_identity.uri,
            "routing_policy_identity": self.routing_policy_identity.uri,
            "promotion_input_audit_identity": self.promotion_input_audit_identity.uri,
            "promotion_tree_identity": self.promotion_tree_identity.uri,
            "qualification_evidence_identity": self.qualification_evidence_identity.uri,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationClosure"
    ) -> ComponentGenerationClosure:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "flavor_set_identity",
                    "skill_set_identity",
                    "workflow_identity",
                    "routing_policy_identity",
                    "promotion_input_audit_identity",
                    "promotion_tree_identity",
                    "qualification_evidence_identity",
                }
            ),
        )
        return cls(
            **{
                field: ContentIdentity.parse_uri(
                    string_value(data[field], f"{path}.{field}")
                )
                for field in (
                    "flavor_set_identity",
                    "skill_set_identity",
                    "workflow_identity",
                    "routing_policy_identity",
                    "promotion_input_audit_identity",
                    "promotion_tree_identity",
                    "qualification_evidence_identity",
                )
            }
        )


def _identity(value: Any, path: str) -> ContentIdentity:
    return ContentIdentity.parse_uri(string_value(value, path))


def _optional_identity(value: Any, path: str) -> ContentIdentity | None:
    return None if value is None else _identity(value, path)


@dataclass(frozen=True, slots=True)
class ComponentAuthorityProjection:
    component_coordinate: str
    component_revision_identity: ContentIdentity | None
    state: ComponentAuthorityState
    transition: ComponentAuthorityTransition
    source_snapshot_identity: ContentIdentity
    specification_set_identity: ContentIdentity | None
    target_lock_identity: ContentIdentity | None
    generation_closure: ComponentGenerationClosure | None
    verifier_identity: ContentIdentity | None
    policy_identity: ContentIdentity | None
    evidence_identities: tuple[ContentIdentity, ...]
    provenance_reference_identity: ContentIdentity
    prior_projection_identity: ContentIdentity | None

    SCHEMA: ClassVar[str] = COMPONENT_AUTHORITY_PROJECTION_SCHEMA

    def __post_init__(self) -> None:
        if (
            not isinstance(self.component_coordinate, str)
            or _COMPONENT_COORDINATE.fullmatch(self.component_coordinate) is None
        ):
            fail(
                "ComponentAuthorityProjection.component_coordinate",
                "must be a canonical component://namespace/name coordinate",
            )
        if not isinstance(self.state, ComponentAuthorityState):
            fail("ComponentAuthorityProjection.state", "must be an authority state")
        if not isinstance(self.transition, ComponentAuthorityTransition):
            fail(
                "ComponentAuthorityProjection.transition",
                "must be an authority transition",
            )
        for field in (
            "component_revision_identity",
            "source_snapshot_identity",
            "specification_set_identity",
            "target_lock_identity",
            "verifier_identity",
            "policy_identity",
            "provenance_reference_identity",
            "prior_projection_identity",
        ):
            value = getattr(self, field)
            if value is None and field not in (
                "source_snapshot_identity",
                "provenance_reference_identity",
            ):
                continue
            if not isinstance(value, ContentIdentity):
                fail(
                    f"ComponentAuthorityProjection.{field}", "must be a ContentIdentity"
                )
        if self.generation_closure is not None and not isinstance(
            self.generation_closure, ComponentGenerationClosure
        ):
            fail(
                "ComponentAuthorityProjection.generation_closure",
                "must be a ComponentGenerationClosure",
            )
        if not self.evidence_identities:
            fail(
                "ComponentAuthorityProjection.evidence_identities", "must not be empty"
            )
        if any(
            not isinstance(item, ContentIdentity) for item in self.evidence_identities
        ):
            fail(
                "ComponentAuthorityProjection.evidence_identities",
                "items must be ContentIdentity values",
            )
        unique(
            self.evidence_identities, "ComponentAuthorityProjection.evidence_identities"
        )
        self._validate_state()

    def _validate_state(self) -> None:
        absent_until_qualified = (
            self.target_lock_identity,
            self.generation_closure,
            self.verifier_identity,
            self.policy_identity,
        )
        if self.state is ComponentAuthorityState.SOURCE_AUTHORITATIVE:
            if self.transition is not ComponentAuthorityTransition.SOURCE_INVENTORY:
                fail(
                    "ComponentAuthorityProjection.transition",
                    "source state requires source-inventory",
                )
            if any(
                item is not None
                for item in (
                    self.component_revision_identity,
                    self.specification_set_identity,
                    *absent_until_qualified,
                    self.prior_projection_identity,
                )
            ):
                fail(
                    "ComponentAuthorityProjection",
                    "source state contains facts not yet established",
                )
            return
        if (
            self.component_revision_identity is None
            or self.specification_set_identity is None
        ):
            fail(
                "ComponentAuthorityProjection",
                "post-derivation states require Component and specification identities",
            )
        if self.prior_projection_identity is None:
            fail(
                "ComponentAuthorityProjection.prior_projection_identity",
                "post-inventory states require a prior projection",
            )
        if self.state is ComponentAuthorityState.SPEC_ASSISTED:
            if self.transition is not ComponentAuthorityTransition.SPEC_DERIVATION:
                fail(
                    "ComponentAuthorityProjection.transition",
                    "spec-assisted state requires spec-derivation",
                )
            if any(item is not None for item in absent_until_qualified):
                fail(
                    "ComponentAuthorityProjection",
                    "spec-assisted state contains qualification facts",
                )
        elif self.state is ComponentAuthorityState.DERIVED_SOURCE_RETAINED:
            if self.transition not in (
                ComponentAuthorityTransition.HUMAN_ACCEPTANCE,
                ComponentAuthorityTransition.QUALIFICATION_INVALIDATION,
            ):
                fail(
                    "ComponentAuthorityProjection.transition",
                    "retained-source state requires acceptance or invalidation",
                )
            if any(item is not None for item in absent_until_qualified):
                fail(
                    "ComponentAuthorityProjection",
                    "retained-source state cannot retain qualification authority",
                )
        else:
            if (
                self.transition
                is not ComponentAuthorityTransition.REGENERATIVE_QUALIFICATION
            ):
                fail(
                    "ComponentAuthorityProjection.transition",
                    "fungible state requires regenerative-qualification",
                )
            if any(item is None for item in absent_until_qualified):
                fail(
                    "ComponentAuthorityProjection",
                    "fungible state requires the exact generation and "
                    "verification closure",
                )

    def require_successor_of(self, prior: ComponentAuthorityProjection) -> None:
        """Validate one complete immutable authority-history edge.

        A projection is not trustworthy merely because its fields match the shape of
        its claimed state.  The predecessor determines both the only legal next
        transition and the facts that must be carried forward unchanged.
        """

        if not isinstance(prior, ComponentAuthorityProjection):
            fail(
                "ComponentAuthorityProjection.prior_projection_identity",
                "predecessor must be a ComponentAuthorityProjection",
            )
        if self.prior_projection_identity != prior.identity:
            fail(
                "ComponentAuthorityProjection.prior_projection_identity",
                "must identify the exact predecessor projection",
            )
        if self.component_coordinate != prior.component_coordinate:
            fail(
                "ComponentAuthorityProjection.component_coordinate",
                "must be carried forward from the predecessor",
            )

        expected = {
            ComponentAuthorityState.SOURCE_AUTHORITATIVE: (
                ComponentAuthorityState.SPEC_ASSISTED,
                ComponentAuthorityTransition.SPEC_DERIVATION,
            ),
            ComponentAuthorityState.SPEC_ASSISTED: (
                ComponentAuthorityState.DERIVED_SOURCE_RETAINED,
                ComponentAuthorityTransition.HUMAN_ACCEPTANCE,
            ),
            ComponentAuthorityState.DERIVED_SOURCE_RETAINED: (
                ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE,
                ComponentAuthorityTransition.REGENERATIVE_QUALIFICATION,
            ),
            ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE: (
                ComponentAuthorityState.DERIVED_SOURCE_RETAINED,
                ComponentAuthorityTransition.QUALIFICATION_INVALIDATION,
            ),
        }[prior.state]
        if (self.state, self.transition) != expected:
            fail(
                "ComponentAuthorityProjection.transition",
                f"{prior.state.value} requires {expected[1].value} into "
                f"{expected[0].value}",
            )

        carried_fields = [
            "source_snapshot_identity",
            "provenance_reference_identity",
        ]
        if prior.state is not ComponentAuthorityState.SOURCE_AUTHORITATIVE:
            carried_fields.extend(
                ("component_revision_identity", "specification_set_identity")
            )
        for field in carried_fields:
            if getattr(self, field) != getattr(prior, field):
                fail(
                    f"ComponentAuthorityProjection.{field}",
                    "must be carried forward from the predecessor",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        def uri(value: ContentIdentity | None) -> str | None:
            return None if value is None else value.uri

        return {
            "schema": self.SCHEMA,
            "component_coordinate": self.component_coordinate,
            "component_revision_identity": uri(self.component_revision_identity),
            "state": self.state.value,
            "transition": self.transition.value,
            "source_snapshot_identity": self.source_snapshot_identity.uri,
            "specification_set_identity": uri(self.specification_set_identity),
            "target_lock_identity": uri(self.target_lock_identity),
            "generation_closure": None
            if self.generation_closure is None
            else self.generation_closure.to_dict(),
            "verifier_identity": uri(self.verifier_identity),
            "policy_identity": uri(self.policy_identity),
            "evidence_identities": [item.uri for item in self.evidence_identities],
            "provenance_reference_identity": self.provenance_reference_identity.uri,
            "prior_projection_identity": uri(self.prior_projection_identity),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentAuthorityProjection"
    ) -> ComponentAuthorityProjection:
        names = frozenset(
            {
                "component_coordinate",
                "component_revision_identity",
                "state",
                "transition",
                "source_snapshot_identity",
                "specification_set_identity",
                "target_lock_identity",
                "generation_closure",
                "verifier_identity",
                "policy_identity",
                "evidence_identities",
                "provenance_reference_identity",
                "prior_projection_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        evidence = tuple(
            _identity(item, f"{path}.evidence_identities[{index}]")
            for index, item in enumerate(
                list_value(data["evidence_identities"], f"{path}.evidence_identities")
            )
        )
        closure = data["generation_closure"]
        return cls(
            component_coordinate=string_value(
                data["component_coordinate"], f"{path}.component_coordinate"
            ),
            component_revision_identity=_optional_identity(
                data["component_revision_identity"],
                f"{path}.component_revision_identity",
            ),
            state=enum_value(ComponentAuthorityState, data["state"], f"{path}.state"),
            transition=enum_value(
                ComponentAuthorityTransition, data["transition"], f"{path}.transition"
            ),
            source_snapshot_identity=_identity(
                data["source_snapshot_identity"], f"{path}.source_snapshot_identity"
            ),
            specification_set_identity=_optional_identity(
                data["specification_set_identity"], f"{path}.specification_set_identity"
            ),
            target_lock_identity=_optional_identity(
                data["target_lock_identity"], f"{path}.target_lock_identity"
            ),
            generation_closure=None
            if closure is None
            else ComponentGenerationClosure.from_dict(
                closure, path=f"{path}.generation_closure"
            ),
            verifier_identity=_optional_identity(
                data["verifier_identity"], f"{path}.verifier_identity"
            ),
            policy_identity=_optional_identity(
                data["policy_identity"], f"{path}.policy_identity"
            ),
            evidence_identities=evidence,
            provenance_reference_identity=_identity(
                data["provenance_reference_identity"],
                f"{path}.provenance_reference_identity",
            ),
            prior_projection_identity=_optional_identity(
                data["prior_projection_identity"], f"{path}.prior_projection_identity"
            ),
        )
