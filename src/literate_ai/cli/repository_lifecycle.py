"""Explicit independent-child lifecycle front door."""

from argparse import Namespace
from pathlib import Path
from typing import Any

from literate_ai.adapters.repository_lifecycle import (
    plan_repository_lifecycle,
    run_repository_lifecycle,
)
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError

from .errors import CliFailure


def repository_lifecycle_from_args(args: Namespace) -> tuple[dict[str, Any], int]:
    try:
        root = Path(args.path)
        declaration = Path(args.declaration)
        selected = tuple(args.only)
        if args.lifecycle_command == "plan":
            return plan_repository_lifecycle(
                root, declaration, args.operation, selected=selected
            ), 0
        result = run_repository_lifecycle(
            root,
            declaration,
            args.operation,
            selected=selected,
            expected_plan_identity=args.expected_plan_identity,
            acknowledged=args.acknowledge,
            resume=None if args.resume is None else Path(args.resume),
        )
        return result, 0 if result["status"] == "passed" else 1
    except OrchestrationInventoryError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise CliFailure(
            "orchestration.lifecycle_invalid",
            "lifecycle inputs or outputs are unavailable or invalid",
        ) from exc
