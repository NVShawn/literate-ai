"""One CLI translation for bounded repository transport deadline arguments."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.contracts import RepositoryFetchDeadlinePolicy

from .errors import CliFailure


@dataclass(frozen=True, slots=True)
class RepositoryFetchDeadlineSelection:
    policy: RepositoryFetchDeadlinePolicy
    provenance: str
    field_provenance: dict[str, str]


def repository_fetch_deadlines_from_args(args) -> RepositoryFetchDeadlineSelection:
    total = getattr(args, "repository_fetch_total_seconds", None)
    no_progress = getattr(args, "repository_fetch_no_progress_seconds", None)
    connect = getattr(args, "repository_fetch_connect_seconds", None)
    provenance = (
        "cli"
        if any(value is not None for value in (total, no_progress, connect))
        else "framework-default"
    )
    fields = {
        "total_seconds": "cli" if total is not None else "framework-default",
        "no_progress_seconds": (
            "cli" if no_progress is not None else "framework-default"
        ),
        "connect_seconds": "cli" if connect is not None else "framework-default",
    }
    defaults = RepositoryFetchDeadlinePolicy()
    try:
        policy = RepositoryFetchDeadlinePolicy(
            total_seconds=defaults.total_seconds if total is None else total,
            no_progress_seconds=(
                defaults.no_progress_seconds if no_progress is None else no_progress
            ),
            connect_seconds=defaults.connect_seconds if connect is None else connect,
        )
    except (TypeError, ValueError) as exc:
        raise CliFailure(
            "repository_lineage.deadline_invalid",
            "repository fetch deadlines must satisfy fixed connect, no-progress, "
            "and total bounds",
        ) from exc
    return RepositoryFetchDeadlineSelection(policy, provenance, fields)


__all__ = [
    "RepositoryFetchDeadlineSelection",
    "repository_fetch_deadlines_from_args",
]
