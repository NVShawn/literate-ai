"""Application orchestration for status and plan-then-apply onboarding."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.operator_adoption import ONBOARD_PLAN_SCHEMA


class OperatorAdoptionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


StatusReader = Callable[[], dict[str, Any]]
PlanBuilder = Callable[[], dict[str, Any]]
ProjectMutator = Callable[[], dict[str, Any]]


class OperatorAdoptionService:
    """Own the operator session while existing services retain all mutations."""

    def __init__(self, *, status_reader: StatusReader | None = None) -> None:
        self._status_reader = status_reader

    def status(self) -> dict[str, Any]:
        if self._status_reader is None:
            raise OperatorAdoptionError(
                "operator.status_unavailable",
                "operator status reader is not configured",
            )
        return self._status_reader()

    def plan(self, mode: str, builder: PlanBuilder) -> dict[str, Any]:
        if mode not in {"create", "adopt"}:
            raise OperatorAdoptionError(
                "operator.onboard_mode_invalid", "onboard mode must be create or adopt"
            )
        details = builder()
        if not isinstance(details, dict):
            raise OperatorAdoptionError(
                "operator.onboard_plan_invalid", "onboard planner returned invalid data"
            )
        reserved = sorted(
            field for field in ("schema", "mode", "plan_identity") if field in details
        )
        if reserved:
            raise OperatorAdoptionError(
                "operator.onboard_plan_invalid",
                "onboard planner returned reserved fields: " + ", ".join(reserved),
            )
        if details.get("writes") is not False:
            raise OperatorAdoptionError(
                "operator.onboard_plan_invalid",
                "onboard planning must explicitly report writes=false",
            )
        if not isinstance(details.get("path"), str) or not details["path"]:
            raise OperatorAdoptionError(
                "operator.onboard_plan_invalid",
                "onboard planning must identify a non-empty project path",
            )
        if not isinstance(details.get("apply_supported"), bool):
            raise OperatorAdoptionError(
                "operator.onboard_plan_invalid",
                "onboard planning must explicitly report apply_supported",
            )
        if not isinstance(details.get("blockers", []), list):
            raise OperatorAdoptionError(
                "operator.onboard_plan_invalid",
                "onboard planning blockers must be a list",
            )
        plan = {**details, "schema": ONBOARD_PLAN_SCHEMA, "mode": mode}
        return {**plan, "plan_identity": canonical_identity(plan).uri}

    def apply(
        self,
        mode: str,
        *,
        builder: PlanBuilder,
        mutator: ProjectMutator,
        acknowledged: bool,
        expected_plan_identity: str | None = None,
    ) -> dict[str, Any]:
        if not acknowledged:
            raise OperatorAdoptionError(
                "operator.onboard_acknowledgement_required",
                "onboard apply requires --acknowledge after reviewing the plan",
            )
        plan = self.plan(mode, builder)
        if expected_plan_identity is not None:
            try:
                expected = ContentIdentity.parse_uri(expected_plan_identity)
            except ValueError as exc:
                raise OperatorAdoptionError(
                    "operator.onboard_plan_identity_invalid",
                    "--expect-plan must be a sha256 content identity",
                ) from exc
            if plan["plan_identity"] != expected.uri:
                raise OperatorAdoptionError(
                    "operator.onboard_plan_changed",
                    "the onboard plan changed after review; inspect the new plan",
                )
        if plan.get("apply_supported") is False:
            raise OperatorAdoptionError(
                "operator.onboard_plan_blocked",
                "the reviewed onboard plan contains blocking findings",
            )
        initialized = mutator()
        if not isinstance(initialized, dict):
            raise OperatorAdoptionError(
                "operator.onboard_result_invalid",
                "onboard mutator returned invalid initialization data",
            )
        result: dict[str, Any] = {
            "schema": "literate-ai/onboard-result@1",
            "mode": mode,
            "applied": True,
            "plan": plan,
            "initialization": initialized,
        }
        return result


__all__ = ["OperatorAdoptionError", "OperatorAdoptionService"]
