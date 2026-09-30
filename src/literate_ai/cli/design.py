"""CLI presentation adapter for intent refinement (ADR 0013)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from literate_ai.application.intent_refinement import (
    IntentRefinementError,
    IntentRefinementService,
)
from literate_ai.contracts.intent_refinement import (
    DesignAcceptRequest,
    DesignDraft,
    DesignStatus,
    IntentRefinementRequest,
)


def design_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from literate_ai.diagnostics import debug_stage

    with debug_stage(f"design.{args.design_command}"):
        return _design_from_args_body(args)


def _design_from_args_body(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    service = IntentRefinementService()
    try:
        if args.design_command == "refine":
            request_path = Path(args.request)
            mission = request_path.read_text(encoding="utf-8")
            request = IntentRefinementRequest(mission=mission, source=str(request_path))
            draft = service.refine(request)
            return draft.to_dict(), 0 if draft.status is DesignStatus.COMPLETE else 1
        if args.design_command == "explain":
            draft = _load_draft(Path(args.draft))
            return (
                service.explain(draft, component=args.component, concern=args.concern),
                0 if draft.status is DesignStatus.COMPLETE else 1,
            )
        if args.design_command == "accept":
            draft = _load_draft(Path(args.draft))
            decisions_path = Path(args.decisions)
            raw = json.loads(decisions_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("schema"):
                accept_request = DesignAcceptRequest.from_dict(raw)
            else:
                accept_request = DesignAcceptRequest.from_dict(
                    {
                        "schema": "literate-ai/design-accept-request@1",
                        "draft_identity": draft.identity.uri,
                        "decisions": raw if isinstance(raw, list) else [],
                    }
                )
            receipt = service.accept(draft, accept_request)
            return receipt.to_dict(), 0
    except IntentRefinementError as exc:
        from .errors import CliFailure

        raise CliFailure(exc.code, exc.message) from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        from .errors import CliFailure

        raise CliFailure("intent_refinement.invalid_input", str(exc)) from exc
    from .errors import CliFailure

    raise CliFailure("cli.usage", "a design command is required")


def add_design_parser(commands: argparse._SubParsersAction) -> None:
    design = commands.add_parser(
        "design",
        help="refine an abstract request into a reviewable design draft",
        description=(
            "Intent refinement is a distinct lifecycle phase before lock and "
            "plan. refine is read-only over the mission text; explain projects "
            "a draft; accept records decisions without invoking generation."
        ),
    )
    design_commands = design.add_subparsers(dest="design_command", required=True)
    refine = design_commands.add_parser(
        "refine",
        help="produce a non-authoritative design draft from a mission request",
    )
    refine.add_argument("request", help="path to the mission request Markdown file")
    explain = design_commands.add_parser(
        "explain",
        help="project a design draft, optionally scoped to one Component or concern",
    )
    explain.add_argument("draft", help="path to a design-draft JSON document")
    explain.add_argument("--component", help="restrict the projection to one Component")
    explain.add_argument("--concern", help="restrict the projection to one concern")
    accept = design_commands.add_parser(
        "accept",
        help="accept a draft's blocking decisions without generating source",
    )
    accept.add_argument("draft", help="path to a design-draft JSON document")
    accept.add_argument(
        "--decisions",
        required=True,
        help="path to a design-accept-request JSON document or decision array",
    )


def _load_draft(path: Path) -> DesignDraft:
    return DesignDraft.from_dict(json.loads(path.read_text(encoding="utf-8")))
