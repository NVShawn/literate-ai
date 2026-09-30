"""`litai skills evaluate`: behavioral regression testing for skill routing.

See `src/literate_ai/skill_routing_evaluation.py` for the evaluation model this wraps.
This is a diagnostic over the routing mechanism itself, not a specification-to-source
skill and not a structural validator -- `litai project validate` already covers schema
shape and DAG acyclicity; this checks whether a skill's own description would actually
distinguish it for a real prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from literate_ai.projects import ProjectError, discover_project
from literate_ai.skill_routing_evaluation import (
    DEFAULT_FIRE_THRESHOLD,
    DEFAULT_FRAGILE_THRESHOLD,
    SkillRoutingEvaluationError,
    evaluate_skill_routing,
)

from .errors import CliFailure

SKILL_ROUTING_EVALUATION_SCHEMA = "literate-ai/skill-routing-evaluation@1"


def skills_evaluate_from_args(args) -> dict[str, Any]:
    """Run a synthetic prompt corpus against the project's real skill catalog."""

    try:
        project = discover_project(Path(getattr(args, "project", ".")))
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if project is None:
        raise CliFailure(
            "project.not_found", "no literate.project.json found from this path"
        )
    fire_threshold = getattr(args, "fire_threshold", None)
    fragile_threshold = getattr(args, "fragile_threshold", None)
    try:
        report = evaluate_skill_routing(
            project,
            Path(args.corpus),
            fire_threshold=(
                DEFAULT_FIRE_THRESHOLD if fire_threshold is None else fire_threshold
            ),
            fragile_threshold=(
                DEFAULT_FRAGILE_THRESHOLD
                if fragile_threshold is None
                else fragile_threshold
            ),
        )
    except SkillRoutingEvaluationError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    return {"schema": SKILL_ROUTING_EVALUATION_SCHEMA, **report.to_dict()}


__all__ = ["SKILL_ROUTING_EVALUATION_SCHEMA", "skills_evaluate_from_args"]
