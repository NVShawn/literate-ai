"""Add a Flavor to an existing project without re-initializing it."""

from __future__ import annotations

import os
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from literate_ai.adapters.component_locks import LOCK_FILENAME
from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.project_initialization import (
    _FLAVOR_CANONICAL_BY_DIRECTORY,
    _TEMPLATE_FILES,
    _template_text,
    canonical_flavor_selector,
    flavor_selector_directory,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    ProjectConfigurationStore,
    ProjectError,
)

FLAVOR_ADD_SCHEMA = "literate-ai/flavor-add@1"
_SKIP_LOCK_PARTS = frozenset(
    {"_build", ".git", ".codegraph", "node_modules", "__pycache__"}
)
_SKILL_FILES_BY_DIRECTORY = {
    "deploy-docker": (
        "skills/specification-to-source/docker-container-application/SKILL.md",
    ),
    "lang-typescript": (
        "skills/specification-to-source/typescript-portable-application/SKILL.md",
    ),
    "lang-zig": ("skills/specification-to-source/zig-portable-application/SKILL.md",),
    "package-npm": ("skills/specification-to-source/javascript-ecosystem/SKILL.md",),
}


class FlavorAddError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def add_flavor_to_project(
    root: Path, selector: str, *, set_default: bool = True
) -> dict[str, Any]:
    """Stamp one Flavor onto an existing project and optionally select it as default."""

    try:
        snapshot = ProjectConfigurationStore.discover(root)
    except ProjectError as exc:
        raise FlavorAddError(exc.code, exc.message) from exc
    if snapshot is None:
        raise FlavorAddError(
            "project.not_found", f"no {PROJECT_FILENAME} found from {root}"
        )
    signed = _require_add_selector(selector)
    canonical = canonical_flavor_selector(signed)
    directory = flavor_selector_directory(signed)
    if directory not in _FLAVOR_CANONICAL_BY_DIRECTORY:
        raise FlavorAddError(
            "flavor.add_unknown",
            f"Flavor {selector!r} is not in the shipped init catalog",
        )
    incoming = _template_authoring(directory)
    store = ProjectConfigurationStore(snapshot.root)
    selected = list(snapshot.definition.default_flavor_selectors)
    if canonical in selected:
        created = _stamp_flavor_files(snapshot.root, directory, overwrite=False)
        if not created:
            return {
                "schema": FLAVOR_ADD_SCHEMA,
                "state": "unchanged",
                "selector": canonical,
                "directory": directory,
                "created": [],
                "locks_invalidated": [],
            }
        locks = _invalidate_component_locks(snapshot.root)
        return {
            "schema": FLAVOR_ADD_SCHEMA,
            "state": "completed",
            "selector": canonical,
            "directory": directory,
            "created": created,
            "locks_invalidated": locks,
        }
    if not set_default:
        # Install files only — no default selection, no conflict check.
        # Used for polyglot projects where per-Component flavor_slots
        # select this Flavor at lock/build time.
        created = _stamp_flavor_files(snapshot.root, directory, overwrite=False)
        locks = _invalidate_component_locks(snapshot.root)
        return {
            "schema": FLAVOR_ADD_SCHEMA,
            "state": "completed",
            "selector": canonical,
            "directory": directory,
            "default": False,
            "created": created,
            "locks_invalidated": locks,
        }
    _reject_conflicts(snapshot, incoming, directory)
    created = _stamp_flavor_files(snapshot.root, directory, overwrite=False)
    updated = replace(
        snapshot.definition,
        default_flavor_selectors=(
            *snapshot.definition.default_flavor_selectors,
            canonical,
        ),
    )
    try:
        store.update(snapshot, updated)
    except ProjectError as exc:
        raise FlavorAddError(exc.code, exc.message) from exc
    locks = _invalidate_component_locks(snapshot.root)
    return {
        "schema": FLAVOR_ADD_SCHEMA,
        "state": "completed",
        "selector": canonical,
        "directory": directory,
        "created": created,
        "locks_invalidated": locks,
    }


def _require_add_selector(selector: str) -> str:
    if not isinstance(selector, str) or not selector.strip():
        raise FlavorAddError(
            "flavor.add_selector_invalid", "Flavor selector is required"
        )
    text = selector.strip()
    if text.startswith("-"):
        raise FlavorAddError(
            "flavor.add_subtract_unsupported",
            "litai flavor add cannot subtract a Flavor",
        )
    return text if text.startswith("+") else f"+{text}"


def _template_authoring(directory: str):
    relative = f"flavors/{directory}/flavor.md"
    return parse_flavor_markdown(
        _template_text(relative).encode("utf-8"), source=relative
    )


def _flavor_identities(authoring, directory: str) -> set[str]:
    coordinate = f"flavor://{authoring.namespace}/{authoring.name}"
    return {
        coordinate,
        authoring.name,
        directory,
        f"flavor://literate-ai/{authoring.name}",
        f"flavor://literate-ai/{directory}",
    }


def _reject_conflicts(project, incoming, directory: str) -> None:
    new_ids = _flavor_identities(incoming, directory)
    new_conflicts = set(incoming.conflicts)
    for current in project.definition.default_flavor_selectors:
        signed = current if current.startswith(("+", "-")) else f"+{current}"
        existing_directory = flavor_selector_directory(signed)
        flavor_path = project.root / "flavors" / existing_directory / "flavor.md"
        if flavor_path.is_file():
            existing = parse_flavor_markdown(
                flavor_path.read_bytes(), source=flavor_path.as_posix()
            )
        else:
            existing = _template_authoring(existing_directory)
        existing_ids = _flavor_identities(existing, existing_directory)
        existing_conflicts = set(existing.conflicts)
        if existing_ids & new_conflicts or new_ids & existing_conflicts:
            raise FlavorAddError(
                "flavor.add_conflict",
                f"{existing.name} conflicts with {incoming.name}; leave the project "
                "unchanged",
            )


def _stamp_flavor_files(root: Path, directory: str, *, overwrite: bool) -> list[str]:
    created: list[str] = []
    resource_root = files("literate_ai.project_template").joinpath("flavors", directory)
    if not resource_root.is_dir():
        raise FlavorAddError(
            "flavor.add_unknown",
            f"Flavor directory {directory!r} is missing from the init template",
        )
    for relative, resource in _walk_template(resource_root, f"flavors/{directory}"):
        destination = root.joinpath(*Path(relative).parts)
        if destination.exists() and not overwrite:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(resource.read_bytes())
        created.append(relative)
    for relative in _SKILL_FILES_BY_DIRECTORY.get(directory, ()):
        if relative not in _TEMPLATE_FILES:
            continue
        destination = root.joinpath(*Path(relative).parts)
        if destination.exists() and not overwrite:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            _template_text(_TEMPLATE_FILES[relative]), encoding="utf-8", newline="\n"
        )
        created.append(relative)
    return created


def _walk_template(resource, prefix: str):
    if resource.is_file():
        yield prefix, resource
        return
    for child in sorted(resource.iterdir(), key=lambda item: item.name):
        child_prefix = f"{prefix}/{child.name}"
        yield from _walk_template(child, child_prefix)


def _invalidate_component_locks(root: Path) -> list[str]:
    invalidated: list[str] = []
    search_roots = [root]
    cell = os.environ.get("LITAI_MATRIX_CELL_ROOT")
    if cell:
        search_roots.append(Path(cell))
    for search in search_roots:
        if not search.is_dir():
            continue
        for lock in search.rglob(LOCK_FILENAME):
            if any(part in _SKIP_LOCK_PARTS for part in lock.parts):
                continue
            if not (lock.parent / "component.md").is_file():
                continue
            lock.unlink()
            try:
                invalidated.append(lock.relative_to(root).as_posix())
            except ValueError:
                invalidated.append(str(lock))
    return invalidated


__all__ = ["FLAVOR_ADD_SCHEMA", "FlavorAddError", "add_flavor_to_project"]
