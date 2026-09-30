"""CLI translation for shared lock orchestration."""

from __future__ import annotations

from typing import Any

from literate_ai.adapters import repository_lock_commands as commands
from literate_ai.adapters.lock_command_errors import LockCommandError

from .errors import CliFailure


def repository_root(selected):
    try:
        return commands.repository_root(selected)
    except LockCommandError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def selected_repository_root(args, selected):
    try:
        return commands.selected_repository_root(args, selected)
    except LockCommandError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def repository_lock_from_args(args, root):
    try:
        return commands.repository_lock_from_args(args, root)
    except LockCommandError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def repository_plan_from_args(args, root):
    try:
        return commands.repository_plan_from_args(args, root)
    except LockCommandError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def repository_lock_check(root):
    try:
        return commands.repository_lock_check(root)
    except LockCommandError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def human_repository_text(result: dict[str, Any]) -> str:
    planning = result["schema"] == "literate-ai/repository-plan@1"
    state = "current" if planning or result["current"] else "not current"
    lines = [f"Literate AI repository {'plan' if planning else 'lock'}: {state}"]
    lines.append(f"Repository lock: {result['repository_lock_identity']}")
    if not planning:
        lines.append(f"Repository artifact: {result['lock']['state']}")
    binding = result["repository_lock"]["repository_orchestration"]
    lines.extend(
        f"  {pin['path']} @ {pin['commit']}" for pin in binding["repositories"]
    )
    lines.append(f"Root-owned Components: {len(result['components'])}")
    if planning or result.get("read_only"):
        lines.append("Read-only; independent child authority is unchanged.")
    else:
        lines.append(
            "Root lock operation only; independent child authority is unchanged."
        )
    lines.append(
        "No child execution, build order, acceptance or publication is implied."
    )
    return "\n".join(lines) + "\n"
