"""Document artifact verification CLI adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from literate_ai.document_pair_verification import verify_document_pair

from .errors import CliFailure


def document_from_args(args: Any) -> tuple[dict[str, object], int]:
    try:
        result = verify_document_pair(Path(args.manifest), Path(args.component))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CliFailure("document.verify_invalid", str(exc)) from exc
    return result, 0 if result["accepted"] else 1


__all__ = ["document_from_args"]
