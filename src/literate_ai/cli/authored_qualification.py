"""Thin authored qualification front door; adapters retain authority ownership."""

from pathlib import Path

from literate_ai.adapters.authored_qualification_planning import (
    FilesystemAuthoredQualificationPlanner,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.projects import discover_project

from .errors import CliFailure


def authored_qualification_from_args(args):
    if args.project_qualify_command != "plan":
        raise CliFailure("cli.usage", "an authored qualification command is required")
    try:
        project = discover_project(Path(args.project))
        if project is None:
            raise CliFailure(
                "project.required",
                "authored qualification requires a canonical project",
            )
        current = FilesystemAuthoredQualificationPlanner().plan(
            project.root,
            GenerationPreparationRequest(
                component_root=project.root / args.component,
                target_name=args.target,
                flavor_selectors=tuple(args.flavor),
                flavor_roots=project.roots("flavor"),
            ),
            profile_path=args.profile,
            output_root=args.output_root,
            pipeline_model=args.model,
        )
        current.require_unchanged()
        plan = current.plan
        return {"plan_identity": plan.identity.uri, "plan": plan.to_dict()}
    except CliFailure:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise CliFailure(
            getattr(exc, "code", "qualification.authored.plan_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc
