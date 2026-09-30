"""Deterministic active-work record and close transformations."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ID = re.compile(r"^[A-Z][A-Z0-9-]{2,63}$")


class RoadmapWorkError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RoadmapWorkRecord:
    work_id: str
    title: str
    priority: str
    owner: str
    direction: str
    conclusion: str
    dependencies: tuple[str, ...]
    implementation: tuple[str, ...]
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.work_id) is None:
            raise RoadmapWorkError("roadmap_work.id_invalid", "work ID is invalid")
        values = (
            self.title,
            self.priority,
            self.owner,
            self.direction,
            self.conclusion,
            *self.dependencies,
            *self.implementation,
            *self.evidence,
        )
        if any(not value.strip() or "\n" in value for value in values):
            raise RoadmapWorkError(
                "roadmap_work.field_invalid", "work fields must be non-empty lines"
            )
        if not self.implementation or not self.evidence:
            raise RoadmapWorkError(
                "roadmap_work.checklist_required",
                "implementation and evidence checklists are required",
            )


def record_work(content: str, record: RoadmapWorkRecord) -> str:
    if re.search(rf"^### \[[ x~]\] {re.escape(record.work_id)}\b", content, re.M):
        raise RoadmapWorkError(
            "roadmap_work.duplicate", f"work item already exists: {record.work_id}"
        )
    dependencies = ", ".join(record.dependencies) if record.dependencies else "none"
    lines = [
        f"### [ ] {record.work_id} — {record.title}",
        "",
        f"- **Priority:** {record.priority}",
        f"- **Owner:** {record.owner}",
        f"- **Direction:** {record.direction}",
        f"- **Conclusion:** {record.conclusion}",
        f"- **Depends on:** {dependencies}",
        "- **Implementation:**",
        *(f"  - [ ] {item}" for item in record.implementation),
        "- **Evidence:**",
        *(f"  - [ ] {item}" for item in record.evidence),
        "",
    ]
    return content.rstrip() + "\n\n" + "\n".join(lines)


def close_work(content: str, work_id: str) -> str:
    if _ID.fullmatch(work_id) is None:
        raise RoadmapWorkError("roadmap_work.id_invalid", "work ID is invalid")
    heading = re.compile(rf"^### \[(?P<state>[ ~x])\] {re.escape(work_id)}\b.*$", re.M)
    match = heading.search(content)
    if match is None:
        raise RoadmapWorkError("roadmap_work.not_found", "work item does not exist")
    if match.group("state") == "x":
        return content
    next_heading = re.search(r"^### ", content[match.end() :], re.M)
    end = (
        match.end() + next_heading.start() if next_heading is not None else len(content)
    )
    section = content[match.end() : end]
    if re.search(r"^\s+- \[[ ~]\] ", section, re.M):
        raise RoadmapWorkError(
            "roadmap_work.incomplete", "work item has unchecked implementation/evidence"
        )
    return content[: match.start("state")] + "x" + content[match.end("state") :]


__all__ = ["RoadmapWorkError", "RoadmapWorkRecord", "close_work", "record_work"]
