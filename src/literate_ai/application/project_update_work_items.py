"""Project an initialized-project update plan into triageable work items.

``litai update`` computes a read-only three-way classification of framework-owned files.
That answers "what differs from upstream" but not "what should this project do about
it", which is the question a derived project's queue exists to hold.

This projection is pure: it turns a plan into work items and decides nothing about the
filesystem. Adoption stays a judgment call, so every item is advisory. Items are derived
only from classifications that represent real upstream movement:

- ``upstream-added``   a capability the project never had; adopting it is optional
- ``upstream-only``    upstream changed a file the project never touched
- ``conflict``         upstream and the project both changed the same file

``unchanged``, ``already-current``, ``local-only``, and ``preserved-dynamic`` describe
files that need no decision, so they produce nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from literate_ai.contracts.project_updates import (
    ProjectUpdateClassification,
    ProjectUpdatePlan,
)

WORK_ITEM_SCHEMA = "urn:literate-ai:schema:v1:project-update-work-item"
WORK_ITEM_PREFIX = "UPSTREAM"

# Conflicts are per-file because each needs its own judgment. The other two are grouped:
# one decision covers the set, and a queue with sixty mechanical entries is not a queue.
_GROUPED = (
    (
        ProjectUpdateClassification.MERGEABLE,
        "Apply {count} clean three-way update merge(s)",
        "Verified baseline/local/upstream text merges cleanly. Apply rechecks "
        "the exact inputs and preserves local edits.",
    ),
    (
        ProjectUpdateClassification.UPSTREAM_ADDED,
        "Consider adopting {count} capability file(s) added upstream",
        "Upstream added files this project has never had. Adopting them is optional; "
        "decide per capability whether this project wants it.",
    ),
    (
        ProjectUpdateClassification.UPSTREAM_ONLY,
        "Review {count} upstream change(s) to untouched framework file(s)",
        "Upstream changed these files and this project never modified them, so they "
        "can usually be taken as-is once reviewed.",
    ),
)


@dataclass(frozen=True, slots=True)
class ProjectUpdateWorkItem:
    """One advisory queue entry derived from an update plan."""

    item_id: str
    title: str
    direction: str
    paths: tuple[str, ...]
    classification: ProjectUpdateClassification

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORK_ITEM_SCHEMA,
            "item_id": self.item_id,
            "title": self.title,
            "direction": self.direction,
            "paths": list(self.paths),
            "classification": self.classification.value,
        }


def _item_id(
    classification: ProjectUpdateClassification, paths: tuple[str, ...]
) -> str:
    """Derive a stable id from the item subject so re-runs do not duplicate it."""

    digest = hashlib.sha256()
    digest.update(classification.value.encode("utf-8"))
    for path in paths:
        digest.update(b"\0")
        digest.update(path.encode("utf-8"))
    return f"{WORK_ITEM_PREFIX}-{digest.hexdigest()[:8].upper()}"


def project_update_work_items(
    plan: ProjectUpdatePlan,
) -> tuple[ProjectUpdateWorkItem, ...]:
    """Return advisory work items for a plan, in a deterministic order."""

    if not isinstance(plan, ProjectUpdatePlan):
        raise TypeError("work-item projection requires a typed update plan")

    items: list[ProjectUpdateWorkItem] = []
    for path in sorted(
        item.path
        for item in plan.files
        if item.classification is ProjectUpdateClassification.CONFLICT
    ):
        paths = (path,)
        items.append(
            ProjectUpdateWorkItem(
                item_id=_item_id(ProjectUpdateClassification.CONFLICT, paths),
                title=f"Resolve upstream conflict in {path}",
                direction=(
                    "Upstream and this project both changed this file. Reconcile them "
                    "deliberately; neither side can be taken wholesale."
                ),
                paths=paths,
                classification=ProjectUpdateClassification.CONFLICT,
            )
        )

    for classification, title, direction in _GROUPED:
        paths = tuple(
            sorted(
                item.path
                for item in plan.files
                if item.classification is classification
            )
        )
        if not paths:
            continue
        items.append(
            ProjectUpdateWorkItem(
                item_id=_item_id(classification, paths),
                title=title.format(count=len(paths)),
                direction=direction,
                paths=paths,
                classification=classification,
            )
        )
    return tuple(items)


__all__ = [
    "WORK_ITEM_PREFIX",
    "WORK_ITEM_SCHEMA",
    "ProjectUpdateWorkItem",
    "project_update_work_items",
]
