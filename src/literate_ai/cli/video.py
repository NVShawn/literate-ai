"""Instructional video CLI: deterministic rendering, no implicit publication."""

from pathlib import Path
from typing import Any

from literate_ai.video_courses import (
    VideoError,
    build_course,
    init_course,
    plan_course,
    verify_course,
)

from .errors import CliFailure


def video_from_args(args: Any) -> tuple[dict[str, Any], int]:
    try:
        if args.video_command == "init":
            result = init_course(Path(args.manifest))
        elif args.video_command == "plan":
            result = plan_course(Path(args.manifest))
        elif args.video_command == "build":
            result = build_course(
                Path(args.manifest),
                Path(args.output),
                font=Path(args.font),
                backend=args.narration,
            )
        else:
            result = verify_course(
                Path(args.receipt),
                manifest=Path(args.manifest) if args.manifest else None,
            )
        return result, 0
    except (VideoError, OSError, ValueError, TypeError, KeyError) as exc:
        raise CliFailure("video.invalid", str(exc)) from exc
