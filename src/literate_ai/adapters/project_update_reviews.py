"""Optional coding-agent review of true ``litai update`` conflicts.

Plan-only: proposals never write the tree. Reuses the existing coding-CLI JSON
task runner rather than a new model-invocation surface.
"""

from __future__ import annotations

from typing import Any, Protocol

from literate_ai.adapters.models.coding_cli import CodingCliError, CodingCliTaskRunner

CONFLICT_REVIEW_SCHEMA = "literate-ai/project-update-conflict-review@1"
REVIEW_DECISIONS = frozenset({"keep-local", "take-upstream", "merge"})


class ConflictReviewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ConflictReviewTaskRunner(Protocol):
    def run_json_task(self, prompt: str, *, model: str | None = None) -> Any: ...


def _review_prompt(diff: dict[str, Any]) -> str:
    path = diff.get("path")
    return (
        "You are reviewing one Literate AI update conflict. Propose exactly one "
        "resolution. Do not invent product behavior. Cite the hunks that motivated "
        "the choice.\n\n"
        "Return strict JSON with keys:\n"
        '  decision: "keep-local" | "take-upstream" | "merge"\n'
        "  rationale: short identity-bound explanation\n"
        "  merged_text: the full merged file when decision is merge, else null\n\n"
        f"Path: {path}\n"
        "Role: inherited catalog or framework template authority file.\n\n"
        "<<<<<<< ours (local overlay)\n"
        f"{diff.get('ours') or ''}"
        "||||||| base (recorded baseline; may be absent)\n"
        f"{diff.get('base') or ''}"
        "=======\n"
        "theirs (upstream at the update target)\n"
        f"{diff.get('theirs') or ''}"
        ">>>>>>> theirs\n"
    )


def review_update_conflicts(
    diffs: list[dict[str, Any]],
    *,
    task_runner: ConflictReviewTaskRunner | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Dispatch one coding-agent review per remaining conflict. Writes nothing."""

    if not diffs:
        return []
    runner = task_runner or CodingCliTaskRunner()
    reviews: list[dict[str, Any]] = []
    for diff in diffs:
        try:
            task = runner.run_json_task(_review_prompt(diff), model=model)
        except CodingCliError as exc:
            raise ConflictReviewError(exc.code, exc.message) from exc
        except (TypeError, ValueError) as exc:
            raise ConflictReviewError(
                "project.update_conflict_review_failed",
                "coding-agent conflict review could not run",
            ) from exc
        response = getattr(task, "response", None)
        if not isinstance(response, dict):
            raise ConflictReviewError(
                "project.update_conflict_review_invalid",
                f"conflict review for {diff.get('path')} was not a JSON object",
            )
        decision = response.get("decision")
        if decision not in REVIEW_DECISIONS:
            raise ConflictReviewError(
                "project.update_conflict_review_invalid",
                f"conflict review for {diff.get('path')} returned an unknown decision",
            )
        rationale = response.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ConflictReviewError(
                "project.update_conflict_review_invalid",
                f"conflict review for {diff.get('path')} omitted its rationale",
            )
        merged = response.get("merged_text")
        if decision == "merge":
            if not isinstance(merged, str):
                raise ConflictReviewError(
                    "project.update_conflict_review_invalid",
                    f"merge review for {diff.get('path')} omitted merged_text",
                )
        else:
            merged = None
        reviews.append(
            {
                "schema": CONFLICT_REVIEW_SCHEMA,
                "path": diff.get("path"),
                "file_identity": diff.get("file_identity"),
                "decision": decision,
                "rationale": rationale.strip(),
                "merged_text": merged,
                "applied": False,
            }
        )
    return reviews


__all__ = [
    "CONFLICT_REVIEW_SCHEMA",
    "ConflictReviewError",
    "REVIEW_DECISIONS",
    "review_update_conflicts",
]
