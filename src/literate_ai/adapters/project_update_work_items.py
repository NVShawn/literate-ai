"""Append update-derived work items to a derived project's existing queue.

The queue is appended to, never rewritten: entries a human has edited, checked off, or
reordered are project authority, and an upstream diff has no business touching them. An
item already present by id is skipped, so re-running is safe and adds nothing new.

The target must already exist and already be part of the documentation graph. Creating a
fresh document would leave it unreachable from the spine and fail project validation, so
this fails closed and says which file it wanted instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.application.project_update_work_items import (
    WORK_ITEM_PREFIX,
    ProjectUpdateWorkItem,
)

DEFAULT_QUEUE_PATH = "docs/roadmap/active-work.md"
SECTION_HEADING = "## Upstream updates"
_MAXIMUM_QUEUE_BYTES = 1024 * 1024


class ProjectUpdateWorkItemError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RecordedWorkItems:
    queue_path: str
    recorded: tuple[str, ...]
    already_present: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_path": self.queue_path,
            "recorded": list(self.recorded),
            "already_present": list(self.already_present),
        }


def _render(item: ProjectUpdateWorkItem) -> str:
    paths = "\n".join(f"  - `{path}`" for path in item.paths)
    return (
        f"### [ ] {item.item_id} — {item.title}\n"
        "\n"
        "- **Owner:** unassigned\n"
        f"- **Direction:** {item.direction}\n"
        "- **Conclusion:** Record the decision, including a decision not to adopt.\n"
        "- **Depends on:** none\n"
        "- **Implementation:**\n"
        f"{paths}\n"
        "- **Evidence:**\n"
        "  - [ ] Name the review or gate that proves the decision was applied.\n"
    )


def record_work_items(
    project_root: Path,
    items: tuple[ProjectUpdateWorkItem, ...],
    *,
    queue_path: str = DEFAULT_QUEUE_PATH,
) -> RecordedWorkItems:
    """Append absent items to the queue and report what was added and what was not."""

    target = project_root.joinpath(*Path(queue_path).parts)
    if not target.is_file():
        raise ProjectUpdateWorkItemError(
            "project_update.queue_missing",
            f"work items require an existing declared queue at {queue_path}; create "
            "and link one from the documentation spine before recording",
        )
    if target.stat().st_size > _MAXIMUM_QUEUE_BYTES:
        raise ProjectUpdateWorkItemError(
            "project_update.queue_too_large",
            f"{queue_path} exceeds the maximum queue size",
        )
    existing = target.read_text(encoding="utf-8")
    present = set(re.findall(rf"\b{WORK_ITEM_PREFIX}-[0-9A-F]{{8}}\b", existing))

    pending = [item for item in items if item.item_id not in present]
    skipped = tuple(item.item_id for item in items if item.item_id in present)
    if not pending:
        return RecordedWorkItems(queue_path, (), skipped)

    body = existing if existing.endswith("\n") else existing + "\n"
    if SECTION_HEADING not in body:
        body += f"\n{SECTION_HEADING}\n\nAdvisory items derived from `litai update`.\n"
    for item in pending:
        body += "\n" + _render(item)
    target.write_text(body, encoding="utf-8")
    return RecordedWorkItems(
        queue_path, tuple(item.item_id for item in pending), skipped
    )


__all__ = [
    "DEFAULT_QUEUE_PATH",
    "SECTION_HEADING",
    "ProjectUpdateWorkItemError",
    "RecordedWorkItems",
    "record_work_items",
]
