"""Precise refresh invalidation over exact inverse-authoring inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from .contracts import (
    SourceToSpecificationRequest,
    SourceToSpecificationResult,
    SpecAuthoringSkillSet,
)


@dataclass(frozen=True, slots=True)
class InvalidationReason:
    object_id: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class RefreshInvalidation:
    invalidated_observation_ids: tuple[str, ...]
    retained_observation_ids: tuple[str, ...]
    invalidated_statement_ids: tuple[str, ...]
    retained_statement_ids: tuple[str, ...]
    invalidated_surface_ids: tuple[str, ...]
    reasons: tuple[InvalidationReason, ...]


def _path_affected(path: str, changes: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    return any(
        candidate == PurePosixPath(changed)
        or PurePosixPath(changed) in candidate.parents
        for changed in changes
    )


def plan_refresh(
    previous: SourceToSpecificationResult,
    new_request: SourceToSpecificationRequest,
    *,
    previous_skill_set: SpecAuthoringSkillSet,
    new_skill_set: SpecAuthoringSkillSet,
    current_evidence_digests: Mapping[str, str],
    changed_source_paths: tuple[str, ...] = (),
) -> RefreshInvalidation:
    """Invalidate provenance affected by source, evidence, skill, or policy drift."""

    old_request = previous.request
    observation_reasons: dict[str, list[InvalidationReason]] = {
        item.observation_id: [] for item in previous.observations
    }
    global_observation_codes: list[tuple[str, str]] = []
    global_draft_codes: list[tuple[str, str]] = []

    if old_request.source_snapshot_id != new_request.source_snapshot_id:
        if changed_source_paths:
            for observation in previous.observations:
                if any(
                    _path_affected(evidence.path, changed_source_paths)
                    for evidence in observation.evidence
                ):
                    observation_reasons[observation.observation_id].append(
                        InvalidationReason(
                            observation.observation_id,
                            "source-path-changed",
                            "observation evidence intersects changed source paths",
                        )
                    )
        else:
            global_observation_codes.append(
                ("source-snapshot-changed", "exact source snapshot changed")
            )
    if old_request.source_content_digest != new_request.source_content_digest and not (
        old_request.source_snapshot_id != new_request.source_snapshot_id
        and changed_source_paths
    ):
        global_observation_codes.append(
            ("source-content-changed", "canonical source content digest changed")
        )
    if old_request.origin_attestation_id != new_request.origin_attestation_id:
        global_observation_codes.append(
            ("origin-attestation-changed", "source origin decision changed")
        )
    for field_name in (
        "routing_policy_id",
        "redaction_policy_id",
        "egress_policy_id",
    ):
        if getattr(old_request, field_name) != getattr(new_request, field_name):
            global_observation_codes.append(
                (f"{field_name.replace('_', '-')}-changed", f"{field_name} changed")
            )
    if old_request.output_provider != new_request.output_provider:
        global_draft_codes.append(
            ("output-provider-changed", "specification output provider changed")
        )
    if (
        old_request.previous_specification_set_id
        != new_request.previous_specification_set_id
    ):
        global_draft_codes.append(
            ("specification-base-changed", "accepted specification base changed")
        )
    if old_request.facets != new_request.facets:
        global_draft_codes.append(("facet-scope-changed", "requested facets changed"))
    if (
        old_request.included_paths != new_request.included_paths
        or old_request.excluded_paths != new_request.excluded_paths
    ):
        global_observation_codes.append(
            ("source-scope-changed", "included or excluded source paths changed")
        )

    old_refs = {item.skill_id: item for item in previous_skill_set.skills}
    new_refs = {item.skill_id: item for item in new_skill_set.skills}
    changed_skills = {
        skill_id
        for skill_id in old_refs.keys() | new_refs.keys()
        if old_refs.get(skill_id) != new_refs.get(skill_id)
    }
    old_order = tuple(item.skill_id for item in previous_skill_set.skills)
    new_order = tuple(item.skill_id for item in new_skill_set.skills)
    if old_order != new_order or (
        previous_skill_set.conflict_policy != new_skill_set.conflict_policy
    ):
        global_observation_codes.append(
            ("skill-order-policy-changed", "skill order or conflict policy changed")
        )
    else:
        for observation in previous.observations:
            if observation.skill.skill_id in changed_skills:
                observation_reasons[observation.observation_id].append(
                    InvalidationReason(
                        observation.observation_id,
                        "skill-identity-changed",
                        "observing skill was added, removed, or changed",
                    )
                )

    for observation in previous.observations:
        for evidence in observation.evidence:
            current = current_evidence_digests.get(evidence.evidence_id)
            if current != evidence.content_digest:
                observation_reasons[observation.observation_id].append(
                    InvalidationReason(
                        observation.observation_id,
                        "evidence-changed",
                        "evidence identity changed or disappeared: "
                        f"{evidence.evidence_id}",
                    )
                )

    for observation_id in observation_reasons:
        observation_reasons[observation_id].extend(
            InvalidationReason(observation_id, code, detail)
            for code, detail in global_observation_codes
        )
    invalidated_observations = {
        observation_id
        for observation_id, reasons in observation_reasons.items()
        if reasons
    }
    all_observations = set(observation_reasons)
    invalidated_statements = {
        statement.statement_id
        for statement in previous.draft.statements
        if set(statement.observation_ids) & invalidated_observations
    }
    if global_draft_codes:
        invalidated_statements = {
            item.statement_id for item in previous.draft.statements
        }
    all_statements = {item.statement_id for item in previous.draft.statements}
    invalidated_surfaces = {
        surface_id
        for observation in previous.observations
        if observation.observation_id in invalidated_observations
        for surface_id in observation.surface_ids
    }
    if global_draft_codes:
        invalidated_surfaces.update(
            item.surface_id for item in previous.coverage.entries
        )

    reasons = [reason for items in observation_reasons.values() for reason in items]
    for statement_id in sorted(invalidated_statements):
        reasons.extend(
            InvalidationReason(statement_id, code, detail)
            for code, detail in global_draft_codes
        )
    return RefreshInvalidation(
        invalidated_observation_ids=tuple(sorted(invalidated_observations)),
        retained_observation_ids=tuple(
            sorted(all_observations - invalidated_observations)
        ),
        invalidated_statement_ids=tuple(sorted(invalidated_statements)),
        retained_statement_ids=tuple(sorted(all_statements - invalidated_statements)),
        invalidated_surface_ids=tuple(sorted(invalidated_surfaces)),
        reasons=tuple(reasons),
    )


__all__ = ["InvalidationReason", "RefreshInvalidation", "plan_refresh"]
