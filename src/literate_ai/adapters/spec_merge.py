"""Staged reverse-adoption of one hand-authored island in an already-managed project.

Distinct from ``litai init --convert``: this never quarantines the tree. It derives a
Component draft from one existing source directory and optionally writes only that
draft.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project
from literate_ai.source_to_specification.inventory import inventory_source
from literate_ai.source_to_specification.skills import (
    builtin_skill_set,
    load_builtin_skill_catalog,
)
from literate_ai.source_to_specification.static_workflow import derive_static_checkout

SPEC_MERGE_PLAN_SCHEMA = "literate-ai/spec-merge-plan@1"
CONVERT_LEGACY_DIRECTORY = "_legacy"
_COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class SpecMergeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _component_id(name: str) -> str:
    normalized = name.strip().lower()
    if not _COMPONENT_NAME.fullmatch(normalized):
        raise SpecMergeError(
            "project.spec_merge_component_invalid",
            "spec-merge Component name must be a lowercase hyphenated identifier",
        )
    return normalized


def _contained(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def plan_spec_merge(
    project_root: Path,
    source: Path,
    *,
    component: str,
) -> dict[str, Any]:
    """Derive one Component draft from an island. Writes nothing."""

    try:
        project = discover_project(Path(project_root))
    except ProjectError as exc:
        raise SpecMergeError(exc.code, exc.message) from exc
    if project is None:
        raise SpecMergeError(
            "project.not_found",
            f"spec merge requires an already-managed project with {PROJECT_FILENAME}",
        )
    root = project.root
    island = Path(source).resolve()
    if not island.exists():
        raise SpecMergeError(
            "project.spec_merge_source_missing",
            "spec-merge source island does not exist",
        )
    if not _contained(root, island):
        raise SpecMergeError(
            "project.spec_merge_source_outside_project",
            "spec-merge source island must stay inside the already-managed project",
        )
    name = _component_id(component)
    destination = f"components/{name}/component.md"
    target = root.joinpath(*Path(destination).parts)
    if target.exists():
        raise SpecMergeError(
            "project.spec_merge_component_exists",
            f"spec-merge refused to overwrite existing {destination}",
        )
    inventory = inventory_source(island if island.is_dir() else island.parent)
    languages = tuple(
        sorted(
            {item.language for item in inventory.entries if item.language is not None}
        )
    )
    catalog = load_builtin_skill_catalog()
    selected = builtin_skill_set(catalog, languages=languages)
    origin_id = f"unverified-local:{inventory.identity.removeprefix('sha256:')}"
    derivation = derive_static_checkout(
        source=island if island.is_dir() else island.parent,
        inventory=inventory,
        origin_attestation_id=origin_id,
        skill_set=selected,
        skill_catalog=catalog,
    )
    body = ""
    for artifact in derivation.result.draft.artifacts:
        if artifact.path.endswith(".md"):
            body = artifact.content
            break
    if not body.strip():
        body = (
            "# Derived specification\n\nNo normative requirement could be supported.\n"
        )
    title = name.replace("-", " ").title()
    document = (
        "---\n"
        f"name: {name}\n"
        f"summary: Reverse-adopted island {title}\n"
        "kind: component\n"
        "inheritable: false\n"
        "---\n"
        f"# {title}\n\n"
        "This Component was reverse-adopted from an already-managed project's "
        "hand-authored island. Existing source remains source authority until "
        "qualification transfers it.\n\n"
        f"{body.lstrip()}"
    )
    return {
        "schema": SPEC_MERGE_PLAN_SCHEMA,
        "mode": "read-only-plan",
        "apply_supported": True,
        "project": str(root),
        "source": island.relative_to(root).as_posix() if island != root else ".",
        "component": name,
        "destination": destination,
        "quarantine": False,
        "legacy_directory_created": False,
        "inventory_identity": inventory.identity,
        "content": document,
    }


def apply_spec_merge(plan: dict[str, Any]) -> dict[str, Any]:
    """Write only the proposed Component draft. Never quarantines."""

    if not isinstance(plan, dict) or plan.get("schema") != SPEC_MERGE_PLAN_SCHEMA:
        raise SpecMergeError(
            "project.spec_merge_plan_invalid", "spec merge apply requires a typed plan"
        )
    root = Path(plan["project"])
    destination = str(plan["destination"])
    content = str(plan["content"])
    target = root.joinpath(*Path(destination).parts)
    if target.exists():
        raise SpecMergeError(
            "project.spec_merge_component_exists",
            f"spec-merge refused to overwrite existing {destination}",
        )
    preexisting_legacy = (root / CONVERT_LEGACY_DIRECTORY).is_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    return {
        "schema": "literate-ai/spec-merge-apply@1",
        "destination": destination,
        "quarantine": False,
        "legacy_directory_created": False,
        "legacy_directory_present": preexisting_legacy,
        "applied": True,
    }


__all__ = [
    "CONVERT_LEGACY_DIRECTORY",
    "SPEC_MERGE_PLAN_SCHEMA",
    "SpecMergeError",
    "apply_spec_merge",
    "plan_spec_merge",
]
