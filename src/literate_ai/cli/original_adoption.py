"""Original-source authority selection delegates to the metadata-only owner."""

import subprocess
from pathlib import Path

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.conversion_authority import ConversionAuthorityError
from literate_ai.adapters.original_project_adoption import (
    OriginalProjectAdoptionError,
    apply_original_project_adoption,
    plan_original_project_adoption,
)

from .errors import CliFailure


def original_adoption_from_args(args):
    from literate_ai.adapters.agent_entrypoints import audit_agent_entrypoints
    from literate_ai.projects import discover_project

    try:
        if args.apply:
            if not args.expected_plan_identity:
                raise CliFailure(
                    "original_adoption.plan-required",
                    "Apply requires the reviewed --expected-plan-identity",
                )
            result = apply_original_project_adoption(
                Path(args.project),
                expected_plan_identity=args.expected_plan_identity,
                acknowledge=args.acknowledge,
            )
            project = discover_project(Path(args.project))
            if project is None:
                raise CliFailure("project.not_found", "canonical project disappeared")
            audit = audit_agent_entrypoints(project)
            return {**result, "agent_entrypoints": audit}
        plan = plan_original_project_adoption(Path(args.project))
        if (
            args.expected_plan_identity
            and args.expected_plan_identity != plan.identity.uri
        ):
            raise CliFailure(
                "original_adoption.plan-changed",
                "Original adoption plan differs from the reviewed identity",
            )
        project = discover_project(Path(args.project))
        if project is None:
            raise CliFailure("project.not_found", "canonical project disappeared")
        audit = audit_agent_entrypoints(project)
        return {
            "schema": "literate-ai/original-adoption-result@1",
            "writes": False,
            "plan_identity": plan.identity.uri,
            "plan": plan.to_dict(),
            "admitted": False,
            "agent_entrypoints": audit,
        }
    except CliFailure:
        raise
    except OriginalProjectAdoptionError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except ConversionAuthorityError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except subprocess.TimeoutExpired as exc:
        raise CliFailure(
            "original_adoption.git-timeout", "Git inspection exceeded its deadline"
        ) from exc
    except (ValueError, OSError, UnsafeFilesystemPathError) as exc:
        raise CliFailure("original_adoption.invalid", str(exc)) from exc
