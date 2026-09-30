"""Read-only CLI adapter for deterministic authority-learning plans."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from literate_ai.application.learning import plan_learning_proposal
from literate_ai.contracts.learning import LearningPlanInput

_MAXIMUM_PLAN_INPUT_BYTES = 4 * 1024 * 1024


def learning_plan_from_args(args: argparse.Namespace) -> dict[str, object]:
    candidate = Path(args.run)
    if candidate.is_symlink():
        raise ValueError("learning run input must not be a symbolic link")
    path = candidate.resolve(strict=True)
    if not path.is_file():
        raise ValueError("learning run input must be a regular file")
    content = path.read_bytes()
    if len(content) > _MAXIMUM_PLAN_INPUT_BYTES:
        raise ValueError("learning run input exceeds the bounded size limit")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("learning run input must be UTF-8 JSON") from exc
    plan_input = LearningPlanInput.from_dict(value)
    proposal = plan_learning_proposal(plan_input)
    return proposal.to_dict()


__all__ = ["learning_plan_from_args"]
