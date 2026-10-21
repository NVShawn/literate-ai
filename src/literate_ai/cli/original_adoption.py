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
    try:
        if args.apply:
            if not args.expected_plan_identity:
                raise CliFailure(
                    "original_adoption.plan-required",
                    "Apply requires the reviewed --expected-plan-identity",
                )
            return apply_original_project_adoption(
                Path(args.project),
                expected_plan_identity=args.expected_plan_identity,
                acknowledge=args.acknowledge,
            )
        plan = plan_original_project_adoption(Path(args.project))
        if (
            args.expected_plan_identity
            and args.expected_plan_identity != plan.identity.uri
        ):
            raise CliFailure(
                "original_adoption.plan-changed",
                "Original adoption plan differs from the reviewed identity",
            )
        return {
            "schema": "literate-ai/original-adoption-result@1",
            "writes": False,
            "plan_identity": plan.identity.uri,
            "plan": plan.to_dict(),
            "admitted": False,
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
