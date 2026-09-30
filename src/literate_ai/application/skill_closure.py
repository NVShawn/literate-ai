"""Deterministic exact transitive closure of specification-to-source skills."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from literate_ai.contracts.identity import (
    ContentIdentity,
    ContentReference,
    HashAlgorithm,
    canonical_identity,
)
from literate_ai.contracts.skills import (
    ResolvedSpecificationToSourceSkill,
    SkillReference,
)

RecipeSkill = ResolvedSpecificationToSourceSkill


class SkillClosureError(ValueError):
    """Selected skills cannot form an exact, stage-compatible closure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def skill_closure_identity(
    skills: Sequence[RecipeSkill],
) -> ContentIdentity:
    """Identify one ordered closed skill set from exact references."""

    return canonical_identity(
        {
            "schema": "literate-ai/skill-closure@1",
            "skills": [skill.ref.to_dict() for skill in skills],
        }
    )


def load_admitted_skill_catalog(project: object) -> tuple[RecipeSkill, ...]:
    """Load every admitted specification-to-source skill from project catalogs."""

    from literate_ai.projects import ProjectError, specification_to_source_skill_paths

    try:
        catalog_paths = specification_to_source_skill_paths(project)
    except ProjectError as exc:
        raise SkillClosureError("skill_closure.catalog_invalid", str(exc)) from exc
    loaded: list[RecipeSkill] = []
    root = Path(project.root)
    for path in catalog_paths:
        reference_uri = path.relative_to(root).as_posix()
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SkillClosureError(
                "skill_closure.catalog_invalid",
                f"admitted skill is unreadable: {reference_uri}",
            ) from exc
        identity = ContentIdentity(
            HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()
        )
        reference = ContentReference(
            "specification-to-source-skill",
            reference_uri,
            identity,
        )
        try:
            loaded.append(
                RecipeSkill.from_reference(
                    reference,
                    content,
                    source=f"project catalog {reference.uri}",
                )
            )
        except (ValueError, UnicodeError) as exc:
            raise SkillClosureError(
                "skill_closure.catalog_invalid",
                f"admitted skill is invalid: {reference_uri}",
            ) from exc
    by_id: dict[str, RecipeSkill] = {}
    for skill in loaded:
        existing = by_id.get(skill.skill_id)
        if existing is not None and existing.ref != skill.ref:
            raise SkillClosureError(
                "skill_closure.catalog_identity_conflict",
                f"admitted skill catalog conflicts on {skill.skill_id!r}",
            )
        by_id[skill.skill_id] = skill
    return tuple(loaded)


def close_specification_to_source_skills(
    selected: Sequence[RecipeSkill],
    catalog: Sequence[RecipeSkill] = (),
    *,
    workflow_stages: Sequence[str] | None = None,
) -> tuple[RecipeSkill, ...]:
    """Expand exact transitive dependencies, unify duplicates, and topo-sort.

    Newly admitted skills keep exact ``SkillReference`` identity. Caller order is
    preserved among already-selected roots; expanded dependencies are inserted
    dependency-first. Cycles, missing catalog authority, identity mismatch, and
    stage incompatibility fail closed.
    """

    catalog_by_id: dict[str, RecipeSkill] = {}
    for skill in catalog:
        existing = catalog_by_id.get(skill.skill_id)
        if existing is not None and existing.ref != skill.ref:
            raise SkillClosureError(
                "skill_closure.catalog_identity_conflict",
                f"skill catalog conflicts on {skill.skill_id!r}",
            )
        catalog_by_id[skill.skill_id] = skill

    ordered_selected: list[RecipeSkill] = []
    by_id: dict[str, RecipeSkill] = {}
    for skill in selected:
        existing = by_id.get(skill.skill_id)
        if existing is None:
            by_id[skill.skill_id] = skill
            ordered_selected.append(skill)
            continue
        if existing.ref != skill.ref:
            raise SkillClosureError(
                "skill_closure.duplicate_conflict",
                f"selected specification-to-source skill is duplicated: "
                f"{skill.skill_id}",
            )

    pending = list(ordered_selected)
    index = 0
    while index < len(pending):
        skill = pending[index]
        index += 1
        _require_stage_compatible(skill, workflow_stages)
        for dependency in skill.dependencies:
            _admit_dependency(
                dependency,
                by_id=by_id,
                catalog_by_id=catalog_by_id,
                pending=pending,
                required_by=skill.skill_id,
            )

    original_index = {
        skill.skill_id: position for position, skill in enumerate(ordered_selected)
    }
    remaining = dict(by_id)
    closed: list[RecipeSkill] = []
    while remaining:
        ready = [
            skill
            for skill in remaining.values()
            if all(
                dependency.skill_id not in remaining
                for dependency in skill.dependencies
            )
        ]
        if not ready:
            raise SkillClosureError(
                "skill_closure.cycle",
                "skill dependency graph has a cycle",
            )
        ready.sort(
            key=lambda skill: (
                original_index.get(skill.skill_id, 10**9),
                skill.skill_id,
            )
        )
        chosen = ready[0]
        closed.append(chosen)
        del remaining[chosen.skill_id]
    return tuple(closed)


def _require_stage_compatible(
    skill: RecipeSkill, workflow_stages: Sequence[str] | None
) -> None:
    if workflow_stages is None:
        return
    admitted = frozenset(workflow_stages)
    unknown = [stage for stage in skill.stages if stage not in admitted]
    if unknown:
        raise SkillClosureError(
            "skill_closure.stage_incompatible",
            f"skill {skill.skill_id!r} declares stages absent from the workflow: "
            + ", ".join(unknown),
        )


def _admit_dependency(
    dependency: SkillReference,
    *,
    by_id: dict[str, RecipeSkill],
    catalog_by_id: dict[str, RecipeSkill],
    pending: list[RecipeSkill],
    required_by: str,
) -> None:
    existing = by_id.get(dependency.skill_id)
    if existing is not None:
        if existing.ref != dependency:
            raise SkillClosureError(
                "skill_closure.identity_mismatch",
                f"skill {required_by!r} requires exact {dependency.skill_id!r}",
            )
        return
    catalog_skill = catalog_by_id.get(dependency.skill_id)
    if catalog_skill is None:
        raise SkillClosureError(
            "skill_closure.dependency_missing",
            f"skill {required_by!r} requires {dependency.skill_id!r}",
        )
    if catalog_skill.ref != dependency:
        raise SkillClosureError(
            "skill_closure.identity_mismatch",
            f"skill {required_by!r} requires exact {dependency.skill_id!r}",
        )
    by_id[dependency.skill_id] = catalog_skill
    pending.append(catalog_skill)


__all__ = [
    "RecipeSkill",
    "SkillClosureError",
    "close_specification_to_source_skills",
    "load_admitted_skill_catalog",
    "skill_closure_identity",
]
