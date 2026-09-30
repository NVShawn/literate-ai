"""Application service for safe initialized-project update planning."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from literate_ai.contracts.project_updates import ProjectUpdatePlan


class ProjectUpdatePlanner(Protocol):
    def plan(self, selected: Path) -> ProjectUpdatePlan: ...


class ProjectUpdateService:
    def __init__(self, planner: ProjectUpdatePlanner) -> None:
        self.planner = planner

    def plan(self, selected: Path) -> ProjectUpdatePlan:
        plan = self.planner.plan(selected)
        if not isinstance(plan, ProjectUpdatePlan):
            raise TypeError("project update planner returned an invalid contract")
        return plan


__all__ = ["ProjectUpdatePlanner", "ProjectUpdateService"]
