"""Typed operator-adoption and brownfield authority-stage contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import contract_fields, enum_value, string_value
from .identity import ContentIdentity, canonical_identity

CONVERSION_AUTHORITY_SCHEMA = "literate-ai/conversion-authority@1"
ONBOARD_PLAN_SCHEMA = "literate-ai/onboard-plan@1"


class ConversionAuthorityStage(StrEnum):
    """Explicit, ordered authority stages for an adopted source tree."""

    WRAPPED = "wrapped"
    RETAINED = "retained"
    DRAFTED = "drafted"
    QUALIFIED = "qualified"


_STAGE_SUCCESSOR = {
    ConversionAuthorityStage.WRAPPED: ConversionAuthorityStage.RETAINED,
    ConversionAuthorityStage.RETAINED: ConversionAuthorityStage.DRAFTED,
    ConversionAuthorityStage.DRAFTED: ConversionAuthorityStage.QUALIFIED,
}


@dataclass(frozen=True, slots=True)
class ConversionAuthorityState:
    """One current, evidence-bound projection of brownfield authority state."""

    project_id: str
    stage: ConversionAuthorityStage
    evidence_identities: tuple[ContentIdentity, ...]
    prior_state_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = CONVERSION_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.project_id, "ConversionAuthorityState.project_id")
        if not isinstance(self.stage, ConversionAuthorityStage):
            raise ValueError("conversion authority stage must be typed")
        if any(
            not isinstance(item, ContentIdentity) for item in self.evidence_identities
        ):
            raise ValueError(
                "conversion authority evidence must use content identities"
            )
        if len({item.uri for item in self.evidence_identities}) != len(
            self.evidence_identities
        ):
            raise ValueError("conversion authority evidence identities must be unique")
        if tuple(sorted(self.evidence_identities, key=lambda item: item.uri)) != (
            self.evidence_identities
        ):
            raise ValueError("conversion authority evidence identities must be sorted")
        if self.stage is ConversionAuthorityStage.WRAPPED:
            if self.prior_state_identity is not None:
                raise ValueError("wrapped is the initial conversion authority stage")
        elif self.prior_state_identity is None:
            raise ValueError(
                "advanced conversion authority requires its prior identity"
            )
        if not self.evidence_identities:
            raise ValueError("conversion authority stage requires exact evidence")

    @property
    def release_authority(self) -> str:
        return (
            "specification"
            if self.stage is ConversionAuthorityStage.QUALIFIED
            else "original-source"
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "project_id": self.project_id,
            "stage": self.stage.value,
            "release_authority": self.release_authority,
            "evidence_identities": [item.uri for item in self.evidence_identities],
            "prior_state_identity": (
                self.prior_state_identity.uri
                if self.prior_state_identity is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Any) -> ConversionAuthorityState:
        data = contract_fields(
            value,
            path="ConversionAuthorityState",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "project_id",
                    "stage",
                    "release_authority",
                    "evidence_identities",
                    "prior_state_identity",
                }
            ),
            optional=frozenset(),
        )
        raw_evidence = data["evidence_identities"]
        if not isinstance(raw_evidence, list):
            raise ValueError("conversion authority evidence identities must be a list")
        state = cls(
            project_id=string_value(
                data["project_id"], "ConversionAuthorityState.project_id"
            ),
            stage=enum_value(
                ConversionAuthorityStage,
                data["stage"],
                "ConversionAuthorityState.stage",
            ),
            evidence_identities=tuple(
                ContentIdentity.parse_uri(
                    string_value(
                        item,
                        f"ConversionAuthorityState.evidence_identities[{index}]",
                    )
                )
                for index, item in enumerate(raw_evidence)
            ),
            prior_state_identity=(
                None
                if data["prior_state_identity"] is None
                else ContentIdentity.parse_uri(
                    string_value(
                        data["prior_state_identity"],
                        "ConversionAuthorityState.prior_state_identity",
                    )
                )
            ),
        )
        if data["release_authority"] != state.release_authority:
            raise ValueError("conversion authority release-authority claim is invalid")
        return state


def advance_conversion_authority(
    current: ConversionAuthorityState,
    stage: ConversionAuthorityStage,
    *,
    evidence_identities: tuple[ContentIdentity, ...],
) -> ConversionAuthorityState:
    """Construct only the next adjacent, evidence-bound conversion state."""

    if not isinstance(current, ConversionAuthorityState):
        raise TypeError("current conversion authority must be typed")
    if _STAGE_SUCCESSOR.get(current.stage) is not stage:
        raise ValueError(
            f"conversion authority cannot advance from {current.stage.value} "
            f"to {stage.value}"
        )
    return ConversionAuthorityState(
        project_id=current.project_id,
        stage=stage,
        evidence_identities=tuple(
            sorted(evidence_identities, key=lambda item: item.uri)
        ),
        prior_state_identity=current.identity,
    )


__all__ = [
    "CONVERSION_AUTHORITY_SCHEMA",
    "ONBOARD_PLAN_SCHEMA",
    "ConversionAuthorityStage",
    "ConversionAuthorityState",
    "advance_conversion_authority",
]
