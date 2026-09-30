"""Roadmap work-item CLI adapter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from literate_ai.application.roadmap_work import (
    RoadmapWorkError,
    RoadmapWorkRecord,
    close_work,
    record_work,
)
from literate_ai.projects import ProjectConfigurationStore

from .errors import CliFailure


def work_from_args(args: Any) -> tuple[dict[str, object], int]:
    project = ProjectConfigurationStore.discover(Path(args.project))
    if project is None:
        raise CliFailure("project.not_found", "no Literate AI project was found")
    path = project.root / "docs/roadmap/active-work.md"
    try:
        content = path.read_text(encoding="utf-8")
        if args.work_command == "record":
            updated = record_work(
                content,
                RoadmapWorkRecord(
                    args.work_id,
                    args.title,
                    args.priority,
                    args.owner,
                    args.direction,
                    args.conclusion,
                    tuple(args.depends_on),
                    tuple(args.implementation),
                    tuple(args.evidence),
                ),
            )
        else:
            updated = close_work(content, args.work_id)
        if updated != content:
            staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            staging.write_text(updated, encoding="utf-8", newline="\n")
            os.replace(staging, path)
    except (OSError, UnicodeError, RoadmapWorkError) as exc:
        raise CliFailure(
            getattr(exc, "code", "roadmap_work.write_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc
    return {
        "schema": "literate-ai/roadmap-work-result@1",
        "operation": args.work_command,
        "work_id": args.work_id,
        "path": path.relative_to(project.root).as_posix(),
        "changed": updated != content,
    }, 0


__all__ = ["work_from_args"]
