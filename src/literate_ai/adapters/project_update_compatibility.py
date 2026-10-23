"""Exact-byte prospective skill closure for read-only update plans and apply."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath

from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)
from literate_ai.contracts import (
    ContentReference,
    ProjectUpdateClassification,
    ProjectUpdateFile,
    ResolvedSpecificationToSourceSkill,
)
from literate_ai.contracts.project_updates import update_adopts_additions
from literate_ai.projects import LoadedProject, discover_project

from .project_updates import ProjectUpdateError, _identity, _local_bytes
from .project_validation import (
    ProjectValidationError,
    validate_resolved_skill_catalog,
)

_CHANGES = frozenset(
    {
        ProjectUpdateClassification.UPSTREAM_ONLY,
        ProjectUpdateClassification.UPSTREAM_ADDED,
        ProjectUpdateClassification.MERGEABLE,
    }
)


def _manifest(project: LoadedProject, relative: str) -> bool:
    path = PurePosixPath(relative)
    return (
        path.name in {"SKILL.md", "skill.json"}
        and any(
            path.is_relative_to(PurePosixPath(root))
            for root in project.definition.skill_roots
        )
    ) or relative == project.definition.agent_skill


def _forward(project: LoadedProject, relative: str) -> bool:
    return any(
        PurePosixPath(relative).is_relative_to(
            PurePosixPath(root) / "specification-to-source"
        )
        for root in project.definition.skill_roots
    )


def _sources(project: LoadedProject) -> dict[str, bytes]:
    paths = AgentSkillCatalog.discover_manifest_paths(
        project.root,
        catalog_roots=project.roots("skill"),
        root_manifests=(project.agent_skill,),
    )
    result = {}
    for path in paths:
        relative = path.relative_to(project.root).as_posix()
        content = _local_bytes(project.root, relative)
        if content is None:
            raise ProjectUpdateError(
                "project.update_local_changed", "skill catalog changed during planning"
            )
        result[relative] = content
    return result


def _overlay(sources, updates, project):
    result = dict(sources)
    for path, content in updates.items():
        if _manifest(project, path):
            if content is None:
                result.pop(path, None)
            else:
                result[path] = content
    return result


def _validate(project: LoadedProject, sources: Mapping[str, bytes]) -> None:
    loaded = []
    locations = {}
    for path, content in sorted(sources.items()):
        if not _forward(project, path):
            continue
        try:
            item = ResolvedSpecificationToSourceSkill.from_reference(
                ContentReference(
                    "specification-to-source-skill", path, _identity(content)
                ),
                content,
                source=f"project catalog {path}",
            )
        except (ValueError, UnicodeError) as exc:
            raise ProjectValidationError(
                "project.skill_invalid",
                f"invalid specification-to-source skill: {path}",
                catalog_paths=(path,),
            ) from exc
        loaded.append(item)
        locations[id(item)] = path
    validate_resolved_skill_catalog(tuple(loaded), locations=locations)
    # The generic taxonomy owns every other skill kind and ambiguous sentinels.
    AgentSkillCatalog.from_documents(sources, validate_references=False)


def classify_skill_compatibility(
    project: LoadedProject,
    files: tuple[ProjectUpdateFile, ...],
    upstream: Mapping[str, bytes],
    *,
    catalog_overrides: Mapping[str, bytes | None] | None = None,
    adopt_added: bool = False,
    resolutions: Mapping | None = None,
) -> tuple[ProjectUpdateFile, ...]:
    """Keep the compatible subset; never replace a pinned dependency to fit it."""
    from .update_merge import resolution_content

    resolutions = resolutions or {}
    adopting = update_adopts_additions(files, explicit=adopt_added)
    candidates = {
        item.path: item
        for item in files
        if (
            item.classification in _CHANGES
            and (
                item.classification is not ProjectUpdateClassification.UPSTREAM_ADDED
                or adopting
            )
            or item.path in resolutions
        )
        and _manifest(project, item.path)
    }
    relevant_overrides = {
        path: content
        for path, content in (catalog_overrides or {}).items()
        if _manifest(project, path)
    }
    if not candidates and not relevant_overrides:
        return files
    try:
        current = _sources(project)
    except (ProjectValidationError, AgentSkillCatalogError) as exc:
        raise ProjectUpdateError(exc.code, exc.message) from exc
    base = _overlay(current, relevant_overrides, project)
    blocked = {}
    while True:
        updates = {
            path: resolution_content(
                item, current.get(path), upstream.get(path), resolutions
            )
            for path, item in candidates.items()
            if path not in blocked
        }
        try:
            _validate(project, _overlay(base, updates, project))
            break
        except (ProjectValidationError, AgentSkillCatalogError) as exc:
            # The owner supplies consumer-first provenance. Prefer refusing a new
            # incompatible consumer; an unchanged consumer instead blocks an
            # incompatible proposed provider replacement or removal.
            selected = next(
                (
                    path
                    for path in exc.catalog_paths
                    if path in candidates and path not in blocked
                ),
                None,
            )
            if selected is None:
                raise ProjectUpdateError(exc.code, exc.message) from exc
            blocked[selected] = (exc.code, exc.message)
    return tuple(replace(item, compatibility=blocked.get(item.path)) for item in files)


def require_selected_skill_closure(
    root: Path, updates: Mapping[str, bytes | None]
) -> None:
    """Recheck the actual selected subset before the first update write."""
    project = discover_project(root)
    if project is None or not any(_manifest(project, path) for path in updates):
        return
    try:
        _validate(project, _overlay(_sources(project), updates, project))
    except (ProjectValidationError, AgentSkillCatalogError) as exc:
        raise ProjectUpdateError(exc.code, exc.message) from exc
