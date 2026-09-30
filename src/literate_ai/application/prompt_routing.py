"""Translate a direct user prompt into one bounded provider task envelope.

This is the deterministic product wiring for ``skills/agent/prompt-master``.
It cannot call a model, mutate locked authority, or authorize execution. MAC
task envelopes bypass it entirely.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from literate_ai.projects import ProjectError, discover_project

PROMPT_TASK_SCHEMA = "literate-ai/prompt-task@1"
MAC_TASK_ENVIRONMENT = "LITAI_MAC_TASK_ID"
PROMPT_MASTER_SKILL = "skills/agent/prompt-master/SKILL.md"


class PromptRoutingError(ValueError):
    """A direct prompt cannot become a bounded provider task."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def translate_direct_prompt(
    request: str,
    *,
    environment: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    component: str | None = None,
    coding_cli: str | None = None,
    mac_envelope: bool = False,
) -> dict[str, Any]:
    """Return one provider-aware task envelope from a rough direct request."""

    configured = os.environ if environment is None else environment
    if mac_envelope or str(configured.get(MAC_TASK_ENVIRONMENT, "")).strip():
        raise PromptRoutingError(
            "prompt_routing.mac_envelope_bypass",
            "MAC already translated this task; do not run prompt-master again",
        )
    outcome = " ".join(request.split())
    if not outcome:
        raise PromptRoutingError(
            "prompt_routing.request_empty",
            "direct prompt translation requires a non-empty request",
        )
    project = None
    if project_root is not None:
        try:
            project = discover_project(project_root)
        except ProjectError as exc:
            raise PromptRoutingError(exc.code, str(exc)) from exc
    authority = [
        "current Component specifications and public interfaces",
        "selected Flavor requirements",
        "exact specification-to-source skills",
        "workflow and routing policy",
        "validation, security, and execution-authorization policy",
    ]
    starting = "inspect the current repository before editing"
    if project is not None:
        starting = (
            f"project root `{project.root.as_posix()}` with "
            f"`{project.definition.agent_skill}` as onboarding"
        )
        if component:
            authority.insert(0, f"Component `{component}` in the current project")
    provider = (coding_cli or configured.get("CODING_CLI", "") or "unselected").strip()
    task = "\n".join(
        (
            "Outcome",
            outcome,
            "",
            "Starting state and exact authority",
            starting,
            "; ".join(authority),
            "",
            "Allowed scope",
            "Only files required to complete the named outcome.",
            "",
            "Forbidden changes",
            "Do not rewrite locked Component, Flavor, skill, workflow, or routing "
            "authority. Do not authorize build, execution, or publication.",
            "",
            "Required work",
            "Inspect the repository, implement the outcome, and verify with evidence.",
            "",
            "Verification",
            "Run the project's existing focused tests or `litai project validate` "
            "for authority-only work.",
            "",
            "Stop and ask before",
            "Destructive git, privilege changes, model egress, or authority mutation.",
            "",
            "Completion report",
            "Name the edited surfaces and the evidence command that passed.",
        )
    )
    return {
        "schema": PROMPT_TASK_SCHEMA,
        "translation": "prompt-master",
        "skill": PROMPT_MASTER_SKILL,
        "provider": provider,
        "outcome": outcome,
        "starting_state": starting,
        "authority": authority,
        "mac_bypass": False,
        "task": task,
    }


__all__ = [
    "MAC_TASK_ENVIRONMENT",
    "PROMPT_MASTER_SKILL",
    "PROMPT_TASK_SCHEMA",
    "PromptRoutingError",
    "translate_direct_prompt",
]
