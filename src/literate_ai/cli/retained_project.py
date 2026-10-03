"""Public retained-project planning, delegated to typed framework owners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.retained_project_planning import plan_retained_project
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectProfile

from .errors import CliFailure
from .execution_workers import select_execution_worker


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Retained authority contains duplicate JSON keys")
        result[key] = value
    return result


def _document(path: Path) -> tuple[bytes, object]:
    if path.absolute() != path.resolve(strict=True) or path_is_link_or_reparse(path):
        raise ValueError("Retained authority may not use redirected paths")
    with path.open("rb") as stream:
        content = stream.read(64 * 1024 * 1024 + 1)
    if len(content) > 64 * 1024 * 1024:
        raise ValueError("Retained authority document exceeds metadata bound")
    return content, json.loads(content, object_pairs_hook=_unique_object)


def retained_project_from_args(
    project: Any, binding: Any, args: Any
) -> dict[str, object]:
    try:
        if not args.retained_project_profile:
            raise ValueError(
                "Retained-project mode requires an explicit locked profile"
            )
        if (
            getattr(args, "retained_source", None)
            or getattr(args, "from_accepted_source", False)
            or args.force_regeneration
            or getattr(args, "update_receipt", False)
        ):
            raise ValueError(
                "Retained authority cannot replace existing source/receipt modes"
            )
        if args.specification != ".":
            raise ValueError("Retained-project admission requires the complete project")
        profile_path = project.root / args.retained_project_profile
        _manifest_bytes, manifest_data = _document(project.root / args.retained_project)
        profile_bytes, profile_data = _document(profile_path)
        manifest = RetainedProjectManifest.from_dict(manifest_data)
        profile = RetainedProjectProfile.from_dict(profile_data)
        worker = select_execution_worker(
            args, project_root=project.root, target_profile=args.target
        )
        plan = plan_retained_project(
            project,
            binding,
            manifest,
            profile,
            worker_identity=worker.worker.identity,
            target=args.target,
            profile_path=profile_path,
            profile_bytes=profile_bytes,
        )
        if args.retained_project_plan:
            return {
                "plan": plan.to_dict(),
                "authorization_identity": plan.identity.uri,
                "admitted": False,
                "release_authority": "original-source",
            }
        plan.require_authorization(args.authorize_retained_project)
        # The planning slice grants no execution/acceptance shortcut while the
        # typed Standard runtime and receipt consumer are being qualified.
        raise CliFailure(
            "retained_project.execution_not_qualified",
            "Retained planning is available; Standard execution is not yet qualified",
        )
    except CliFailure:
        raise
    except (ValueError, TypeError, OSError) as exc:
        raise CliFailure("retained_project.admission_invalid", str(exc)) from exc
