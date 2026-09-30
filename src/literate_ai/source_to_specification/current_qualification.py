"""Current lifecycle-bound regenerative qualification evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from .contracts import canonical_digest
from .errors import SourceToSpecificationError
from .qualification import CleanRegenerationEvidence, RegenerativeQualificationPolicy

CURRENT_REGENERATIVE_QUALIFICATION_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v3:regenerative-qualification-evidence"
)
_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise SourceToSpecificationError(
            "current_qualification.identity_invalid",
            f"{field} must be a sha256 content identity",
        )
    return value


@dataclass(frozen=True, slots=True)
class CurrentRegenerativeQualificationEvidence:
    """Self-contained evidence admitted by the current authority lifecycle."""

    source_snapshot_identity: str
    specification_set_identity: str
    component_revision_identity: str
    target_lock_identity: str
    flavor_set_identity: str
    skill_set_identity: str
    workflow_identity: str
    routing_policy_identity: str
    promotion_input_audit_identity: str
    promotion_tree_identity: str
    inverse_evidence_custody_identity: str
    generation_recipe_identity: str
    regenerator_identity: str
    verifier_identity: str
    attestor_identity: str
    policy: RegenerativeQualificationPolicy
    runs: tuple[CleanRegenerationEvidence, ...]

    SCHEMA: ClassVar[str] = CURRENT_REGENERATIVE_QUALIFICATION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        identity_fields = (
            "source_snapshot_identity",
            "specification_set_identity",
            "component_revision_identity",
            "target_lock_identity",
            "flavor_set_identity",
            "skill_set_identity",
            "workflow_identity",
            "routing_policy_identity",
            "promotion_input_audit_identity",
            "promotion_tree_identity",
            "inverse_evidence_custody_identity",
            "generation_recipe_identity",
            "regenerator_identity",
            "verifier_identity",
            "attestor_identity",
        )
        for field in identity_fields:
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        if not isinstance(self.policy, RegenerativeQualificationPolicy):
            raise SourceToSpecificationError(
                "current_qualification.policy_invalid",
                "policy must be a regenerative qualification policy",
            )
        if not self.runs or any(
            not isinstance(item, CleanRegenerationEvidence) for item in self.runs
        ):
            raise SourceToSpecificationError(
                "current_qualification.runs_invalid",
                "runs must contain typed clean-regeneration evidence",
            )
        if len({item.run_id for item in self.runs}) != len(self.runs) or len(
            {item.run_attestation_id for item in self.runs}
        ) != len(self.runs):
            raise SourceToSpecificationError(
                "current_qualification.runs_duplicate",
                "qualification run and attestation identities must be unique",
            )
        providers = (
            self.regenerator_identity,
            self.verifier_identity,
            self.attestor_identity,
        )
        if len(set(providers)) != len(providers):
            raise SourceToSpecificationError(
                "current_qualification.providers_not_independent",
                "regenerator, verifier, and attestor must be distinct",
            )
        self._require_complete()

    def _require_complete(self) -> None:
        for run in self.runs:
            if (
                run.source_snapshot_id != self.source_snapshot_identity
                or run.specification_set_id != self.specification_set_identity
                or run.flavor_lock_id != self.target_lock_identity
                or run.generation_recipe_id != self.generation_recipe_identity
            ):
                raise SourceToSpecificationError(
                    "current_qualification.binding_mismatch",
                    "a clean run describes another source, specification, lock, "
                    "or recipe",
                )
            if not (
                run.source_excluded_from_generation
                and run.empty_workspace
                and not run.generated_source_cache_hit
                and run.build_passed
                and run.generated_tests_passed
                and run.independent_parity_passed
                and run.generated_tests_failed == 0
                and (
                    self.policy.allow_skipped_tests or run.generated_tests_skipped == 0
                )
            ):
                raise SourceToSpecificationError(
                    "current_qualification.run_failed",
                    "every admitted run must be clean, source-free, built, tested, "
                    "and parity-passing",
                )
        for target in self.policy.required_target_profile_ids:
            target_runs = tuple(
                item for item in self.runs if item.target_profile_id == target
            )
            if len(target_runs) < self.policy.minimum_clean_runs:
                raise SourceToSpecificationError(
                    "current_qualification.runs_insufficient",
                    "the policy minimum clean-run count was not met",
                )
            covered = {
                surface for item in target_runs for surface in item.covered_surface_ids
            }
            if not set(self.policy.required_surface_ids).issubset(covered):
                raise SourceToSpecificationError(
                    "current_qualification.coverage_incomplete",
                    "clean runs do not cover every policy-required surface",
                )

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "source_snapshot_identity": self.source_snapshot_identity,
            "specification_set_identity": self.specification_set_identity,
            "component_revision_identity": self.component_revision_identity,
            "target_lock_identity": self.target_lock_identity,
            "flavor_set_identity": self.flavor_set_identity,
            "skill_set_identity": self.skill_set_identity,
            "workflow_identity": self.workflow_identity,
            "routing_policy_identity": self.routing_policy_identity,
            "promotion_input_audit_identity": self.promotion_input_audit_identity,
            "promotion_tree_identity": self.promotion_tree_identity,
            "inverse_evidence_custody_identity": self.inverse_evidence_custody_identity,
            "generation_recipe_identity": self.generation_recipe_identity,
            "regenerator_identity": self.regenerator_identity,
            "verifier_identity": self.verifier_identity,
            "attestor_identity": self.attestor_identity,
            "policy": self.policy.to_dict(),
            "runs": [item.to_dict() for item in self.runs],
        }

    @classmethod
    def from_dict(cls, value: object) -> CurrentRegenerativeQualificationEvidence:
        identity_fields = (
            "source_snapshot_identity",
            "specification_set_identity",
            "component_revision_identity",
            "target_lock_identity",
            "flavor_set_identity",
            "skill_set_identity",
            "workflow_identity",
            "routing_policy_identity",
            "promotion_input_audit_identity",
            "promotion_tree_identity",
            "inverse_evidence_custody_identity",
            "generation_recipe_identity",
            "regenerator_identity",
            "verifier_identity",
            "attestor_identity",
        )
        expected = {"schema", *identity_fields, "policy", "runs"}
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value.get("schema") != cls.SCHEMA
        ):
            raise SourceToSpecificationError(
                "current_qualification.wire_invalid",
                "current qualification evidence has missing or unknown fields",
            )
        raw_runs = value["runs"]
        if not isinstance(raw_runs, list):
            raise SourceToSpecificationError(
                "current_qualification.wire_invalid", "runs must be an array"
            )
        return cls(
            **{field: value[field] for field in identity_fields},
            policy=RegenerativeQualificationPolicy.from_dict(
                value["policy"], path="current_qualification.policy"
            ),
            runs=tuple(
                CleanRegenerationEvidence.from_dict(
                    item, path=f"current_qualification.runs[{index}]"
                )
                for index, item in enumerate(raw_runs)
            ),
        )  # type: ignore[arg-type]


__all__ = [
    "CURRENT_REGENERATIVE_QUALIFICATION_EVIDENCE_SCHEMA",
    "CurrentRegenerativeQualificationEvidence",
]
