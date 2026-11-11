"""Thin authored qualification front door; adapters retain authority ownership."""

from pathlib import Path

from literate_ai.adapters.authored_qualification_planning import (
    FilesystemAuthoredQualificationPlanner,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.projects import discover_project

from .errors import CliFailure


def authored_qualification_from_args(args):
    if args.project_qualify_command not in {"plan", "apply"}:
        raise CliFailure("cli.usage", "an authored qualification command is required")
    try:
        project = discover_project(Path(args.project))
        if project is None:
            raise CliFailure(
                "project.required",
                "authored qualification requires a canonical project",
            )
        selection = GenerationPreparationRequest(
            component_root=project.root / args.component,
            target_name=args.target,
            flavor_selectors=tuple(args.flavor),
            flavor_roots=project.roots("flavor"),
        )
        current = FilesystemAuthoredQualificationPlanner().plan(
            project.root,
            selection,
            profile_path=args.profile,
            output_root=args.output_root,
            pipeline_model=args.model,
        )
        current.require_unchanged()
        plan = current.plan
        if args.project_qualify_command == "apply":
            from literate_ai.adapters.authored_qualification_publication import (
                apply_authored_qualification,
            )

            published = apply_authored_qualification(
                current,
                selection,
                acknowledged_plan=ContentIdentity.parse_uri(args.acknowledge_plan),
                allow_host_execution=args.allow_host_execution,
                scratch_parent=None
                if args.scratch_parent is None
                else Path(args.scratch_parent),
                recover_attempt=args.recover_attempt,
                max_parallelism=args.max_parallelism,
                max_bytes=args.max_bytes,
            )
            return {
                "qualified": True,
                "origin": "authored",
                "reference_identity": published.reference.identity.uri,
                "reference": published.reference.to_dict(),
                "reference_path": published.reference_path,
                "archive_store_path": published.archive_store_path,
            }
        return {"plan_identity": plan.identity.uri, "plan": plan.to_dict()}
    except CliFailure:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise CliFailure(
            getattr(exc, "code", "qualification.authored.plan_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc
