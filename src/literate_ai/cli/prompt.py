"""`litai prompt translate`: wrap prompt-master for direct agent work."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from literate_ai.application.prompt_routing import (
    PROMPT_TASK_SCHEMA,
    PromptRoutingError,
    translate_direct_prompt,
)

from .errors import CliFailure


def prompt_translate_from_args(args) -> dict[str, Any]:
    """Translate one direct request into a bounded provider task envelope."""

    request = " ".join(getattr(args, "request", ()) or ())
    if getattr(args, "file", None):
        request = Path(args.file).read_text(encoding="utf-8")
    try:
        envelope = translate_direct_prompt(
            request,
            project_root=Path(getattr(args, "project", ".")),
            component=getattr(args, "component", None),
            coding_cli=getattr(args, "coding_cli", None),
            external_envelope=bool(getattr(args, "external_envelope", False)),
        )
    except PromptRoutingError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except OSError as exc:
        raise CliFailure(
            "prompt_routing.request_unreadable",
            "direct prompt file could not be read",
        ) from exc
    return {"schema": PROMPT_TASK_SCHEMA, **envelope}


__all__ = ["prompt_translate_from_args"]
