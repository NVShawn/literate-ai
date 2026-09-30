"""Read-only current lock observations shared by verification and HTML sources."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.projects import ProjectError, discover_project

from .lock_command_errors import LockCommandError

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class LockHealthGate:
    gate: str
    state: str
    detail: str


@dataclass(frozen=True, slots=True)
class LockHealthObservation:
    """Internal detailed result from the verifier's existing lock checks.

    Serialization is a derived current-state report, not an admitted attestation.
    A repository failure deliberately leaves Components unobserved, matching verify.
    """

    gate: LockHealthGate
    repository: dict[str, Any] | None
    components: tuple[dict[str, Any], ...]
    project_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "urn:literate-ai:schema:v2:project-lock-health",
            "project_id": self.project_id,
            "gate": {
                "gate": self.gate.gate,
                "state": self.gate.state,
                "detail": self.gate.detail,
            },
            "repository": self.repository,
            "components": list(self.components),
        }


def observe_project_locks(root: Path) -> LockHealthObservation:
    """Verify committed locks only.

    Verify checks declared state. A Component with no committed lock has nothing to
    verify, and resolving one from scratch would demand a full selector set that a
    multi-language repository cannot supply from a single target -- that is `litai lock`
    doing work, not `verify` reading.
    """

    from .component_lock_commands import component_lock_from_args

    project_id: str | None = None
    repository: dict[str, Any] | None = None
    components: list[dict[str, Any]] = []

    def observation(state: str, detail: str) -> LockHealthObservation:
        return LockHealthObservation(
            LockHealthGate("locks", state, detail),
            repository,
            tuple(components),
            project_id,
        )

    # Scope to declared component roots. Scratch trees such as _build/ contain throwaway
    # projects whose locks are not this project's declared state.
    try:
        project = discover_project(root)
    except ProjectError as exc:
        return observation(FAIL, f"{exc.code}: {exc.message}")
    if project is None:
        return observation(FAIL, "project not found")
    project_id = project.definition.project_id
    repository_locked = project.definition.repository_orchestration is not None
    if repository_locked:
        from .repository_lock_commands import repository_lock_check

        try:
            check = repository_lock_check(project.root)
            repository = check
        except LockCommandError as exc:
            return observation(FAIL, f"{exc.code}: {exc.message}")
        if check["state"] != "current":
            return observation(
                FAIL,
                f"repository lock is {check['state']}; run litai lock on this root",
            )
    locked = sorted(
        path.parent
        for catalog in project.definition.component_roots
        for path in (project.root / catalog).rglob("component.lock.json")
        if path.is_file() and (path.parent / "component.md").is_file()
    )
    if not locked:
        if repository_locked:
            return observation(
                PASS,
                "repository lock current; no committed root Component locks",
            )
        return observation(SKIPPED, "no Component declares a committed lock to verify")
    stale: list[str] = []
    for component in locked:
        try:
            report, status = component_lock_from_args(
                Namespace(
                    component=str(component),
                    target="host",
                    flavor=[],
                    flavor_root=[],
                    check=True,
                    diff=False,
                )
            )
        except LockCommandError as exc:
            components.append(
                {
                    "component": component.relative_to(project.root).as_posix(),
                    "state": "error",
                    "report": None,
                    "error": {"code": exc.code, "message": exc.message},
                }
            )
            stale.append(f"{component.name}: {exc.code}")
            continue
        components.append(
            {
                "component": component.relative_to(project.root).as_posix(),
                "state": "current" if status == 0 else "not-current",
                "report": report,
                "error": None,
            }
        )
        if status != 0:
            stale.append(f"{component.name}: not current")
    if stale:
        return observation(FAIL, "; ".join(stale))
    prefix = "repository lock and " if repository_locked else ""
    return observation(PASS, f"{prefix}{len(locked)} committed lock(s) current")
