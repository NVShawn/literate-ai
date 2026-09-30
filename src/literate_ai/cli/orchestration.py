"""Scoped orchestration front door; no flat-conversion dispatch or child execution."""

from argparse import Namespace
from pathlib import Path
from typing import Any

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.orchestration_initialization import (
    check_orchestration_initialization,
    initialize_orchestration,
    plan_orchestration_initialization,
)
from literate_ai.adapters.orchestration_planning import (
    check_orchestration,
    plan_orchestration,
)
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_planning import (
    apply_planned_repository_refresh,
    check_repository_refresh,
    load_repository_refresh_request,
    plan_repository_refresh,
)

from .errors import CliFailure
from .repository_fetch_deadlines import repository_fetch_deadlines_from_args


def orchestration_from_args(args: Namespace) -> dict[str, Any]:
    try:
        root = Path(args.path)
        if args.orchestration_command == "refresh":
            request = load_repository_refresh_request(Path(args.request))
            policy = repository_fetch_deadlines_from_args(args).policy
            if args.refresh_command == "apply":
                return apply_planned_repository_refresh(
                    root,
                    request,
                    expected_plan_identity=args.expected_plan_identity,
                    acknowledged=args.acknowledge,
                    deadline_policy=policy,
                )
            if args.refresh_command == "check":
                return check_repository_refresh(
                    root,
                    request,
                    expected_plan_identity=args.expected_plan_identity,
                    deadline_policy=policy,
                )
            return plan_repository_refresh(root, request, deadline_policy=policy)
        declaration = Path(args.declaration)
        project_id, version = args.project_id, args.project_version
        if (project_id is None) != (version is None):
            raise CliFailure(
                "orchestration.project_required",
                "initialization plans require both project ID and version",
            )
        if args.orchestration_command == "initialize":
            return initialize_orchestration(
                root,
                declaration,
                project_id=project_id,
                version=version,
                expected_plan_identity=args.expected_plan_identity,
                acknowledged=args.acknowledge,
            )
        if project_id is not None:
            if args.orchestration_command == "check":
                return check_orchestration_initialization(
                    root,
                    declaration,
                    project_id=project_id,
                    version=version,
                    expected_plan_identity=args.expected_plan_identity,
                )
            return plan_orchestration_initialization(
                root, declaration, project_id=project_id, version=version
            )
        if args.orchestration_command == "check":
            return check_orchestration(
                root, declaration, expected_plan_identity=args.expected_plan_identity
            )
        return plan_orchestration(root, declaration)
    except OrchestrationInventoryError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except CliFailure:
        raise
    except (ValueError, TypeError) as exc:
        raise CliFailure(
            "orchestration.inputs_invalid",
            str(exc) or "orchestration inputs are invalid",
        ) from exc
    except OSError as exc:
        raise CliFailure(
            "orchestration.inputs_invalid",
            str(exc) or "orchestration inputs are unavailable",
        ) from exc
    except UnsafeFilesystemPathError as exc:
        raise CliFailure(
            "orchestration.inputs_invalid",
            str(exc) or "orchestration inputs are unsafe",
        ) from exc


def human_orchestration_text(result: dict[str, Any]) -> str:
    if result.get("schema") == "literate-ai/orchestration-refresh-plan@1":
        return (
            "Literate AI orchestration refresh: reviewed plan\n"
            f"Plan: {result['plan_identity']}\n"
            f"Changes: {len(result['changes'])}\n"
            "Child source writes: "
            f"{'required' if result['source_writes'] else 'none'}\n"
            "Read-only plan; publication verified and child authority remains "
            "independent.\n"
            "Child acceptance and crash replay are not qualified.\n"
        )
    if result.get("schema") == "literate-ai/orchestration-refresh-check@1":
        return (
            "Literate AI orchestration refresh: current\n"
            f"Plan: {result['plan_identity']}\n"
            "Read-only check; child acceptance is not qualified.\n"
        )
    if result.get("schema") == "literate-ai/orchestration-refresh-apply@1":
        authority_changed = result.get(
            "authority_changed", result.get("changed", False)
        )
        filesystem_writes = result.get(
            "filesystem_writes", result.get("writes", authority_changed)
        )
        if authority_changed:
            outcome = (
                "Manifest committed last; child authority remains independent.\n"
                "Review the changed root documentation authority before the "
                "next refresh.\n"
            )
        elif result.get("source_writes"):
            outcome = (
                "Root authority is unchanged; reviewed child source custody was "
                "transitioned.\n"
            )
        elif filesystem_writes:
            outcome = (
                "Root authority is unchanged, but terminal cleanup artifacts were "
                "retained; persistent filesystem writes remain.\n"
            )
        else:
            outcome = "Exact no-op; no repository files or object packs were written.\n"
        return (
            f"Literate AI orchestration refresh: {result['state']}\n"
            f"Plan: {result['plan_identity']}\n"
            f"Child source writes: {'yes' if result['source_writes'] else 'no'}\n"
            + outcome
            + "Child acceptance and crash replay are not qualified.\n"
            + (
                "Commit succeeded; terminal staging was retained after cleanup "
                "could not be proven complete. It is evidence only, not replay "
                "authority.\n"
                if result.get("cleanup_retained")
                else ""
            )
        )
    if result.get("writes") is True:
        return (
            "Literate AI orchestration: initialized\n"
            f"Plan: {result['plan_identity']}\n"
            "Root-only additions; child authority remains independent.\n"
            "Child execution, acceptance and publication are not qualified.\n"
            + (
                "Changed staging files were preserved; inspect cleanup_retained.\n"
                if result.get("cleanup_retained")
                else ""
            )
        )
    return (
        f"Literate AI orchestration: {result.get('state', 'plan')}\n"
        f"Plan: {result['plan_identity']}\n"
        "Read-only; child authority remains independent.\n"
        "Initialization, execution and publication are not qualified by this result.\n"
    )
