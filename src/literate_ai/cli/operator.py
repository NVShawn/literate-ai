"""CLI adapters for operator status, onboarding, and conversion authority."""

from __future__ import annotations

import os
import shlex
from argparse import Namespace
from pathlib import Path
from typing import Any

from literate_ai.adapters.conversion_authority import (
    CONVERSION_AUTHORITY_FILE,
    ConversionAuthorityError,
    FilesystemConversionAuthorityStore,
    evidence_for_conversion_stage,
)
from literate_ai.adapters.lifecycle_lock import (
    ProjectLifecycleLockError,
    project_lifecycle_lock,
)
from literate_ai.adapters.operator_status import (
    OperatorStatusError,
    inspect_host_preflight,
    inspect_operator_status,
)
from literate_ai.adapters.project_initialization import (
    ProjectInitializationError,
    apply_init_flavor_defaults,
    detect_repo_flavors,
    plan_convert,
)
from literate_ai.application.intent_refinement import (
    IntentRefinementError,
    IntentRefinementService,
)
from literate_ai.application.operator_adoption import (
    OperatorAdoptionError,
    OperatorAdoptionService,
)
from literate_ai.contracts.intent_refinement import (
    DesignStatus,
    IntentRefinementRequest,
)
from literate_ai.contracts.operator_adoption import ConversionAuthorityStage
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project

from .errors import CLI_ERROR_MESSAGE_CHARS, CliFailure
from .project import init_project_from_args


def _translate_error(
    exc: Exception, *, message_limit: int = CLI_ERROR_MESSAGE_CHARS
) -> CliFailure:
    return CliFailure(
        getattr(exc, "code", "operator.invalid_input"),
        getattr(exc, "message", str(exc)),
        message_limit=message_limit,
    )


def operator_status_from_args(args: Namespace) -> dict[str, Any]:
    try:
        if args.command == "doctor":
            return {
                "schema": "literate-ai/operator-status@1",
                "view": "doctor",
                "project": None,
                "host": inspect_host_preflight(),
                "next_verb": "litai onboard create|adopt",
            }
        service = OperatorAdoptionService(
            status_reader=lambda: inspect_operator_status(
                Path(args.project), view=args.command
            )
        )
        return service.status()
    except (OperatorAdoptionError, OperatorStatusError) as exc:
        raise _translate_error(exc) from exc


def conversion_authority_from_args(args: Namespace) -> dict[str, Any]:
    if args.convert_stage_command != "show":
        project = discover_project(Path(args.project))
        if project is not None:
            try:
                with project_lifecycle_lock(
                    project.root, operation="convert-stage.advance"
                ):
                    return _conversion_authority_from_args(args)
            except ProjectLifecycleLockError as exc:
                raise _translate_error(exc) from exc
    return _conversion_authority_from_args(args)


def retained_scope_from_args(args: Namespace) -> dict[str, Any]:
    from literate_ai.adapters.retained_scope_refresh import (
        apply_retained_scope_refresh,
        plan_retained_scope_refresh,
    )

    try:
        if args.apply:
            if not args.expected_plan_identity:
                raise CliFailure(
                    "retained_scope.plan_required",
                    "apply requires the reviewed --expected-plan-identity",
                )
            return apply_retained_scope_refresh(
                Path(args.project),
                expected_plan_identity=args.expected_plan_identity,
                acknowledge=args.acknowledge,
                run_component_baselines=bool(
                    getattr(args, "run_component_baselines", False)
                ),
            )
        return plan_retained_scope_refresh(Path(args.project))
    except CliFailure:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        raise _translate_error(exc) from exc


def retained_harness_from_args(args: Namespace) -> dict[str, Any]:
    from literate_ai.adapters.retained_harness_readmission import (
        apply_retained_harness_readmission,
        plan_retained_harness_readmission,
    )

    try:
        from .project import _select_retained_execution_worker

        worker, catalog_identity = _select_retained_execution_worker(
            args, Path(args.project).resolve()
        )
        if args.apply:
            if not args.expected_plan_identity:
                raise CliFailure(
                    "retained_harness.plan_required",
                    "apply requires the reviewed --expected-plan-identity",
                )
            return apply_retained_harness_readmission(
                Path(args.project),
                expected_plan_identity=args.expected_plan_identity,
                acknowledge=args.acknowledge,
                timeout_seconds=args.timeout_seconds,
                execution_worker=worker,
                worker_catalog_identity=catalog_identity,
                retained_stage_ids=tuple(args.retain_stage),
            )
        return plan_retained_harness_readmission(
            Path(args.project),
            execution_worker=worker,
            worker_catalog_identity=catalog_identity,
            timeout_seconds=args.timeout_seconds,
            retained_stage_ids=tuple(args.retain_stage),
        )
    except CliFailure:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        raise _translate_error(exc, message_limit=CLI_ERROR_MESSAGE_CHARS * 64) from exc


def _conversion_authority_from_args(args: Namespace) -> dict[str, Any]:
    try:
        project = discover_project(Path(args.project))
        if project is None:
            raise CliFailure(
                "project.not_found", f"no {PROJECT_FILENAME} found from {args.project}"
            )
        store = FilesystemConversionAuthorityStore(project.root)
        current = store.load_optional()
        if current is None:
            raise ConversionAuthorityError(
                "conversion_authority.not_adopted",
                "project has no conversion authority; only adopted projects "
                "have stages",
            )
        if current.project_id != project.definition.project_id:
            raise ConversionAuthorityError(
                "conversion_authority.project_mismatch",
                "conversion authority identifies another project",
            )
        if args.convert_stage_command == "show":
            state = current
            operation = "show"
        else:
            stage = ConversionAuthorityStage(args.to)
            evidence = evidence_for_conversion_stage(project, stage)
            state = store.advance(stage, evidence_identities=evidence)
            operation = "advance"
        return {
            **state.to_dict(),
            "schema": "literate-ai/conversion-authority-command@1",
            "operation": operation,
            "project": str(project.root),
            "path": CONVERSION_AUTHORITY_FILE,
            "identity": state.identity.uri,
        }
    except CliFailure:
        raise
    except (ConversionAuthorityError, ProjectError, ValueError) as exc:
        raise _translate_error(exc) from exc


def _path_blockers(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_dir():
        return [
            {
                "code": "project.init_target_invalid",
                "detail": "target is not a directory",
            }
        ]
    blockers: list[dict[str, str]] = []
    for item in sorted(path.iterdir(), key=lambda candidate: candidate.name):
        if item.name == ".git" and item.is_dir() and not item.is_symlink():
            continue
        if item.name == "README.md" and item.is_file() and not item.is_symlink():
            continue
        blockers.append(
            {
                "code": "project.init_target_not_empty",
                "detail": f"existing entry requires adopt instead: {item.name}",
            }
        )
    return blockers


def _create_plan(args: Namespace) -> dict[str, Any]:
    path = Path(args.path).expanduser().resolve()
    blockers = _path_blockers(path)
    selectors = apply_init_flavor_defaults(tuple(args.flavors or ()))
    design = None
    if args.refine is not None:
        request_path = Path(args.refine)
        mission = request_path.read_text(encoding="utf-8")
        draft = IntentRefinementService().refine(
            IntentRefinementRequest(mission=mission, source=str(request_path))
        )
        design = draft.to_dict()
        if draft.status is not DesignStatus.COMPLETE:
            blockers.append(
                {
                    "code": "operator.onboard_design_incomplete",
                    "detail": "design refinement has unresolved blocking questions",
                }
            )
    return {
        "path": str(path),
        "writes": False,
        "apply_supported": not blockers,
        "blockers": blockers,
        "host": inspect_host_preflight(flavor_selectors=selectors),
        "initialization": {
            "project_id": args.project_id or path.name,
            "project_type": args.project_type or "application",
            "empty": bool(args.empty),
            "flavor_selectors": list(selectors),
            "source_intelligence_provider": args.source_intelligence_provider,
            "repository_from": args.repository_from,
        },
        "design_refinement": design,
        "landing_stage": None,
        "next_commands": [
            f"litai status --project {shlex.quote(os.fspath(path))}",
            f"litai verify {shlex.quote(os.fspath(path))}",
            f"litai rebuild --project {shlex.quote(os.fspath(path))}"
            " --allow-host-execution",
        ],
    }


def _adopt_plan(args: Namespace) -> dict[str, Any]:
    configured = Path(args.path).expanduser()
    if configured.is_symlink():
        raise ProjectInitializationError(
            "project.init_target_invalid",
            "convert target must be a direct project directory, not a symlink",
        )
    path = configured.resolve(strict=True)
    root_plan = getattr(args, "root_plan", None)
    conversion = plan_convert(
        path,
        default_branch=args.default_branch,
        **(
            {"root_plan": Path(root_plan).expanduser()} if root_plan is not None else {}
        ),
    )
    detected = detect_repo_flavors(path)
    selectors = apply_init_flavor_defaults(tuple(args.flavors or detected))
    readiness = conversion["readiness"]
    apply_supported = readiness == "ready" or (
        readiness == "blocked-missing-catalog" and bool(args.allow_unready)
    )
    blockers = [] if apply_supported else [readiness]
    refinement = conversion.get("root_refinement")
    if root_plan is not None:
        # Never route a refined request through the single-wrapper mutator.
        refinement_supported = bool(
            isinstance(refinement, dict) and refinement.get("apply_supported") is True
        )
        if not refinement_supported:
            blockers.extend(
                refinement["blockers"]
                if isinstance(refinement, dict)
                else ["monorepo.component_materialization_unavailable"]
            )
        if not args.run_baseline:
            blockers.append("monorepo.baseline_execution_required")
        apply_supported = (
            apply_supported and refinement_supported and bool(args.run_baseline)
        )
    return {
        "path": str(path),
        "writes": False,
        "apply_supported": apply_supported,
        "blockers": blockers,
        "host": inspect_host_preflight(flavor_selectors=selectors),
        "conversion": conversion,
        "initialization": {
            "project_id": args.project_id or path.name,
            "flavor_selectors": list(selectors),
            "run_baseline": bool(args.run_baseline),
            "allow_unready": bool(args.allow_unready),
            "repository_from": args.repository_from,
        },
        "landing_stage": "wrapped" if apply_supported else None,
        "next_commands": []
        if root_plan is not None
        else [
            f"litai status --project {shlex.quote(os.fspath(path))}",
            "litai project test-receipt run-retained CANDIDATE --project "
            + shlex.quote(os.fspath(path)),
            "litai project convert-stage advance --to retained --project "
            + shlex.quote(os.fspath(path)),
        ],
    }


def _init_namespace(args: Namespace, *, convert: bool) -> Namespace:
    return Namespace(
        path=args.path,
        project_id=args.project_id,
        profile="canonical",
        source_intelligence_provider=args.source_intelligence_provider,
        empty=bool(getattr(args, "empty", False)),
        project_type=getattr(args, "project_type", None),
        no_tool_bootstrap=False,
        flavors=list(args.flavors or ()),
        convert=convert,
        convert_plan=False,
        run_baseline=bool(getattr(args, "run_baseline", False)),
        baseline_timeout_seconds=getattr(args, "baseline_timeout_seconds", None),
        baseline_diagnostic_chars=getattr(args, "baseline_diagnostic_chars", None),
        harness_workspace_link=list(getattr(args, "harness_workspace_link", ())),
        allow_unready=bool(getattr(args, "allow_unready", False)),
        repository_from=args.repository_from,
        default_branch=getattr(args, "default_branch", None),
        root_plan=getattr(args, "root_plan", None),
        repository_fetch_total_seconds=getattr(
            args, "repository_fetch_total_seconds", None
        ),
        repository_fetch_no_progress_seconds=getattr(
            args, "repository_fetch_no_progress_seconds", None
        ),
        repository_fetch_connect_seconds=getattr(
            args, "repository_fetch_connect_seconds", None
        ),
    )


def onboard_from_args(args: Namespace) -> dict[str, Any]:
    mode = args.onboard_command
    builder = (
        (lambda: _create_plan(args))
        if mode == "create"
        else (lambda: _adopt_plan(args))
    )
    try:
        if not args.apply and (args.acknowledge or args.expect_plan is not None):
            raise OperatorAdoptionError(
                "operator.onboard_apply_flag_required",
                "--acknowledge and --expect-plan are valid only with --apply",
            )
        service = OperatorAdoptionService(
            status_reader=(
                (lambda: inspect_operator_status(Path(args.path), view="status"))
                if bool(args.apply)
                else None
            )
        )
        if not args.apply:
            return service.plan(mode, builder)
        result = service.apply(
            mode,
            builder=builder,
            mutator=lambda: init_project_from_args(
                _init_namespace(args, convert=mode == "adopt")
            ),
            acknowledged=bool(args.acknowledge),
            expected_plan_identity=args.expect_plan,
        )
        try:
            result["status"] = service.status()
        except (OperatorAdoptionError, OperatorStatusError) as exc:
            # Initialization has already completed. Preserve that observable fact
            # rather than reporting the whole mutating command as if it rolled back.
            result["status"] = {
                "state": "unavailable-after-apply",
                "error": {
                    "code": getattr(exc, "code", "operator.status_unavailable"),
                    "message": getattr(exc, "message", str(exc)),
                },
            }
        return result
    except (
        IntentRefinementError,
        OperatorAdoptionError,
        OperatorStatusError,
        ProjectInitializationError,
        OSError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise _translate_error(exc) from exc


def human_operator_status_text(result: dict[str, Any]) -> str:
    project = result.get("project") or {}
    host = result.get("host") or {}
    conversion = project.get("conversion_authority") or {}
    coding = (host.get("coding_cli") or {}) if isinstance(host, dict) else {}
    tools = (host.get("host_tools") or {}) if isinstance(host, dict) else {}
    if result.get("view") == "doctor":
        paths = (host.get("paths") or {}) if isinstance(host, dict) else {}
        return "\n".join(
            (
                "Literate AI doctor",
                f"Host paths: {paths.get('state', 'unknown')}",
                "Host tools: "
                f"{'ready' if tools.get('ready') else 'attention required'}",
                f"Coding CLI: {coding.get('name', 'none')} "
                f"({coding.get('state', 'unknown')})",
                f"Next: {result.get('next_verb', 'unavailable')}",
                "",
            )
        )
    lines = [
        f"Literate AI {result.get('view', 'status')}: "
        f"{project.get('project_id', 'unknown')}",
        f"Project: {project.get('root', 'unknown')} ({project.get('kind', 'unknown')})",
        f"Authority: {(project.get('authority') or {}).get('state', 'unknown')}",
        f"Conversion stage: {conversion.get('stage', 'not-adopted')}",
        f"Locks: {(project.get('locks') or {}).get('state', 'unknown')}",
        f"Receipt: {(project.get('test_receipt') or {}).get('state', 'unknown')}",
        f"Host tools: {'ready' if tools.get('ready') else 'attention required'}",
        f"Coding CLI: {coding.get('name', 'none')} ({coding.get('state', 'unknown')})",
        f"Next: {result.get('next_verb', 'unavailable')}",
    ]
    if not tools.get("ready"):
        lines.append(
            "Host readiness is separate from project authority; run litai doctor "
            "before building. Passing verify does not qualify the host toolchain."
        )
    return "\n".join(lines) + "\n"


def human_onboard_text(result: dict[str, Any]) -> str:
    plan = result.get("plan") if result.get("applied") else result
    if not isinstance(plan, dict):
        plan = {}
    lines = [
        f"Literate AI onboard {plan.get('mode', result.get('mode', 'unknown'))}: "
        + ("applied" if result.get("applied") else "plan"),
        f"Project: {plan.get('path', 'unknown')}",
        f"Plan: {plan.get('plan_identity', 'unknown')}",
        f"Landing stage: {plan.get('landing_stage') or 'created-project'}",
    ]
    blockers = plan.get("blockers") or []
    if blockers:
        lines.append(f"Blockers: {len(blockers)}")
    if not result.get("applied"):
        lines.append("Review this plan, then rerun with --apply --acknowledge.")
    for command in plan.get("next_commands") or []:
        lines.append(f"Next: {command}")
    return "\n".join(lines) + "\n"


def human_conversion_authority_text(result: dict[str, Any]) -> str:
    return (
        f"Conversion authority: {result.get('stage')} "
        f"({result.get('release_authority')})\n"
        f"Evidence: {len(result.get('evidence_identities') or [])} identity(s)\n"
    )


__all__ = [
    "conversion_authority_from_args",
    "human_conversion_authority_text",
    "human_onboard_text",
    "human_operator_status_text",
    "onboard_from_args",
    "operator_status_from_args",
]
