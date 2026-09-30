"""CLI boundary for the shared read-only project verifier."""

from pathlib import Path
from typing import Any

from literate_ai.adapters.project_verification import (
    GATES,
    VERIFY_SCHEMA,
    GateResult,
    ProjectVerificationError,
    observe_project_verification,
)
from literate_ai.adapters.project_verification import (
    _locks as _locks,
)

from .errors import CliFailure


def verify_project_from_args(args) -> tuple[dict[str, Any], int]:
    try:
        return observe_project_verification(
            Path(args.path), gates=getattr(args, "gates", None)
        )
    except ProjectVerificationError as exc:
        raise CliFailure(exc.code, exc.message) from exc


__all__ = ["GATES", "VERIFY_SCHEMA", "GateResult", "verify_project_from_args"]
