"""CLI translation for shared lock orchestration."""

from __future__ import annotations

from literate_ai.adapters import component_lock_commands as commands
from literate_ai.adapters.lock_command_errors import LockCommandError

from .errors import CliFailure


def component_lock_from_args(args):
    try:
        return commands.component_lock_from_args(args)
    except LockCommandError as exc:
        raise CliFailure(exc.code, exc.message) from exc
