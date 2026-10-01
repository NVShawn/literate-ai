"""The obvious first path: build a Component, then run it.

Writing, compiling, and running "hello, world" is the first page of every C textbook.
A project initialized from this framework had no equivalent: `rebuild` reads as
something you do *again*, and nothing at all named a way to execute the result, because
the runtime holding it was deleted on the way out.

`build` and `run` are that pair. They are deliberately thin: `build` drives the same
gated lifecycle machinery and then persists a small durable export, and `run` executes
exactly the command that export recorded. Neither invents a second lifecycle.

`build` and `rebuild` differ in intent, not in machinery. `build` produces an artifact
from the project's current specifications -- the ordinary inner loop. `rebuild` is
reconciliation after a `litai update`: re-run the coding agent over new plan entries,
regenerate source where the update made it stale, then build. Reach for `rebuild` when
upstream has moved; reach for `build` the rest of the time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

from literate_ai.adapters.artifact_exports import (
    ArtifactExportError,
    available_exports,
    load_artifact_export,
    record_artifact_export,
    record_remote_artifact_export,
)
from literate_ai.adapters.execution_dispatch import (
    CommandExecutionDispatcher,
    ExecutionDispatchAdapterError,
    SshExecutionDispatcher,
)
from literate_ai.adapters.ssh_execution import SshLifecycleRequestHandler
from literate_ai.contracts import (
    DispatchResultStatus,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
)
from literate_ai.diagnostics import inherited_verbose_environment, trace_subprocess
from literate_ai.project_source_index import (
    ProjectSourceIntelligenceError,
    require_lifecycle_project_index,
)
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project

from .errors import CliFailure
from .execution_workers import (
    admit_execution_worker_health,
    create_execution_dispatch_request,
    dispatch_with_worker_health_poll,
    select_execution_worker,
)

BUILD_SCHEMA = "literate-ai/component-build@2"
RUN_SCHEMA = "literate-ai/component-run@2"
TEST_SCHEMA = "literate-ai/component-test@1"


def _project(path: str):
    try:
        project = discover_project(Path(path))
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if project is None:
        raise CliFailure(
            "project.not_found", f"no {PROJECT_FILENAME} found from {path}"
        )
    return project


def _project_root(path: str) -> Path:
    return _project(path).root


def _component_argument(args: Any, project: Any) -> str:
    explicit = getattr(args, "component", None)
    if explicit:
        return explicit
    configured = project.definition.repository_policy.default_component
    if configured:
        return configured
    raise CliFailure(
        "project.default_component_missing",
        "component is required because repository_policy.default_component is null",
    )


def _build_from_args(args: Any, *, action: LifecycleDispatchAction) -> dict[str, Any]:
    """Run the gated lifecycle, then persist the artifact so `run` can find it."""

    from .rebuild import rebuild_from_args

    project = _project(getattr(args, "project", "."))
    root = project.root
    component = _component_argument(args, project)
    target_profile = getattr(args, "target", "host")
    selected_worker = select_execution_worker(
        args, project_root=root, target_profile=target_profile
    )
    dispatch_request = create_execution_dispatch_request(
        args,
        project_root=root,
        component=component,
        selected=selected_worker,
        action=action,
    )
    health = admit_execution_worker_health(
        args, selected_worker, dispatch_request.identity
    )
    if selected_worker.worker.kind in {
        ExecutionWorkerKind.COMMAND,
        ExecutionWorkerKind.SSH,
    }:
        try:
            dispatcher = (
                CommandExecutionDispatcher()
                if selected_worker.worker.kind is ExecutionWorkerKind.COMMAND
                else SshExecutionDispatcher(SshLifecycleRequestHandler())
            )
            dispatched, health_polls = dispatch_with_worker_health_poll(
                args,
                selected_worker,
                dispatch_request.identity,
                lambda: dispatcher.dispatch(
                    selected_worker.worker,
                    dispatch_request,
                    cwd=root,
                ),
            )
            coverage_gap_report = dispatched.coverage_gaps
            blocking = _unimplemented_surface_message(coverage_gap_report)
            if blocking is not None:
                raise CliFailure("build.unimplemented_surface", blocking)
            if dispatched.status is not DispatchResultStatus.PASSED:
                admit_execution_worker_health(
                    args, selected_worker, dispatch_request.identity
                )
                raise CliFailure(
                    "execution.dispatch_failed",
                    f"worker dispatch ended with {dispatched.status.value}",
                )
            assert dispatched.artifact_reference is not None
            export = record_remote_artifact_export(
                root,
                component,
                artifact_reference=dispatched.artifact_reference,
                target_profile=target_profile,
                worker=selected_worker.worker,
                dispatch_request=dispatch_request,
                library_product=dispatched.library_product,
            )
        except ExecutionDispatchAdapterError as exc:
            admit_execution_worker_health(
                args, selected_worker, dispatch_request.identity
            )
            raise CliFailure(exc.code, exc.message) from exc
        except ArtifactExportError as exc:
            raise CliFailure(exc.code, exc.message) from exc
        response = {
            "schema": BUILD_SCHEMA,
            "component": component,
            "target_profile": target_profile,
            "execution_worker": {
                "worker_id": selected_worker.worker.worker_id,
                "worker_identity": selected_worker.worker.identity.uri,
                "catalog_identity": selected_worker.catalog_identity.uri,
            },
            "passed": True,
            "artifact": export.artifact_reference.uri,
            "artifact_identity": export.artifact_reference.identity.uri,
            "run": f"litai run {export.component}",
            "test_summary": None,
            "dispatch": dispatched.to_dict(),
            "project_source_intelligence": {
                "database_identity": dispatch_request.source_index_identity.uri
            },
            "coverage_gaps": coverage_gap_report,
        }
        if dispatched.library_product is not None:
            response.pop("run")
            response["library_artifact"] = dispatched.library_product.to_dict()
        if health is not None:
            response["worker_health"] = health
            response["worker_health_polls"] = list(health_polls)
        return response
    # Retain the runtime only long enough to copy the artifact out of it. The dispatch
    # request owns accepted-source provider authority: the admitting session environment
    # may already be gone when this local rebuild starts.
    request = Namespace(
        specification=component,
        project=str(root),
        runtime_root=None,
        candidate_receipt=None,
        update_receipt=getattr(args, "update_receipt", False),
        keep_runtime=True,
        jobs=getattr(args, "jobs", None),
        flavor=list(getattr(args, "flavor", []) or []),
        target=target_profile,
        execution_worker=selected_worker,
        force_regeneration=getattr(args, "force_regeneration", False),
        model=getattr(args, "model", None),
        source_cache_entry=list(getattr(args, "source_cache_entry", None) or []),
        source_cache_root=[],
        allow_host_execution=True,
        from_accepted_source=bool(getattr(args, "from_accepted_source", False)),
        accepted_source_provider_id=dispatch_request.accepted_source_provider_id,
        accepted_source_provider_identity=(
            dispatch_request.accepted_source_provider_identity
        ),
    )
    result = rebuild_from_args(request)

    runtime_root = result.get("runtime_root")
    coverage_gap_report = _scan_coverage_gaps(root, component, runtime_root)
    try:
        blocking = _unimplemented_surface_message(coverage_gap_report)
        if blocking is not None:
            raise CliFailure("build.unimplemented_surface", blocking)
        library_product = None
        if "library_artifact" in result:
            from literate_ai.contracts.library_products import LibraryArtifactProduct

            if result.get("passed") is not True:
                raise CliFailure(
                    "build.library_not_accepted", "library lifecycle did not pass"
                )
            try:
                library_product = LibraryArtifactProduct.from_dict(
                    result["library_artifact"]
                )
            except (TypeError, ValueError) as exc:
                raise CliFailure(
                    "build.library_artifact_invalid",
                    "library product authority is invalid",
                ) from exc
        export = record_artifact_export(
            root,
            component,
            artifact=Path(str(result.get("artifact"))),
            execution_command=dict(result.get("execution_command") or {}),
            execution_entrypoints=tuple(
                dict(item) for item in result.get("execution_entrypoints", [])
            ),
            target_profile=target_profile,
            worker=selected_worker.worker,
            dispatch_request=dispatch_request,
            library_product=library_product,
        )
    except ArtifactExportError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    finally:
        # The runtime was retained for the copy only; it is not authority and not kept.
        if runtime_root and not getattr(args, "keep_runtime", False):
            import shutil

            shutil.rmtree(runtime_root, ignore_errors=True)

    response = {
        "schema": BUILD_SCHEMA,
        "component": component,
        "target_profile": target_profile,
        "execution_worker": {
            "worker_id": selected_worker.worker.worker_id,
            "worker_identity": selected_worker.worker.identity.uri,
            "catalog_identity": selected_worker.catalog_identity.uri,
        },
        "passed": bool(result.get("passed")),
        "artifact": str(export.artifact),
        "run": f"litai run {export.component}",
        "test_summary": result.get("test_summary"),
        "project_source_intelligence": result.get("project_source_intelligence"),
        "coverage_gaps": (
            None if coverage_gap_report is None else coverage_gap_report.to_dict()
        ),
    }
    retained_entrypoints = tuple(getattr(export, "entrypoints", ()))
    if library_product is not None:
        response.pop("run")
        response["library_artifact"] = library_product.to_dict()
        response["artifact_identity"] = export.artifact_reference.identity.uri
    if retained_entrypoints:
        response["entrypoints"] = [item.name for item in retained_entrypoints]
        response["run"] = (
            f"litai run {export.component} --entrypoint "
            f"{getattr(export, 'default_entrypoint', retained_entrypoints[0].name)}"
        )
    return response


def _unimplemented_surface_message(report: object) -> str | None:
    from literate_ai.adapters.coverage_gaps import unimplemented_surface_message

    if report is None:
        return None
    return unimplemented_surface_message(report)


def _scan_coverage_gaps(
    project_root: Path, component: str, runtime_root: object
) -> Any:
    """Structural scan for declared-but-unimplemented generated surface (#64).

    Best-effort: only runs when a generated-source tree is available. High-confidence
    markers fail the build via ``build.unimplemented_surface``; scan-side defects
    return ``None`` and never fail the build.
    """

    from literate_ai.adapters.coverage_gaps import scan_runtime_coverage_gaps

    return scan_runtime_coverage_gaps(project_root, component, runtime_root)


def build_from_args(args: Any) -> dict[str, Any]:
    """Build through the selected exact worker."""

    return _build_from_args(args, action=LifecycleDispatchAction.BUILD)


def _demonstration_arguments(
    root: Path, component: str, entrypoint: str | None = None
) -> tuple[list[str], str]:
    """Fall back to the Component's first declared acceptance case, if it has one."""

    from literate_ai.adapters.component_acceptance import (
        ComponentAcceptanceError,
        load_declared_cases,
        oracle_path,
    )

    try:
        _bound, cases = load_declared_cases(oracle_path(root, component, entrypoint))
    except ComponentAcceptanceError:
        return [], "none"
    if not cases:
        return [], "none"
    return [json.dumps(cases[0].arguments, separators=(",", ":"))], (
        f"acceptance case {cases[0].case_id!r}"
    )


def test_from_args(args: Any) -> dict[str, Any]:
    """Require the current build, generated tests, and acceptance gates to pass."""

    built = _build_from_args(args, action=LifecycleDispatchAction.TEST)
    tested = {
        "schema": TEST_SCHEMA,
        "component": built["component"],
        "target_profile": built["target_profile"],
        "execution_worker": built["execution_worker"],
        "passed": built["passed"],
        "test_summary": built["test_summary"],
        "artifact": built["artifact"],
        "project_source_intelligence": built["project_source_intelligence"],
    }
    if "artifact_identity" in built:
        tested["artifact_identity"] = built["artifact_identity"]
    if "dispatch" in built:
        tested["dispatch"] = built["dispatch"]
    if "library_artifact" in built:
        tested["library_artifact"] = built["library_artifact"]
    return tested


test_from_args.__test__ = False  # not a test: named after the `litai test` CLI verb,
# matching the sibling build_from_args/run_from_args naming convention. pytest collects
# any imported test_*-named callable by default, so without this it gets picked up as a
# bogus test wherever it's imported (e.g. test_cli_command_worker_lifecycle.py).


def run_from_args(args: Any) -> tuple[dict[str, Any], int]:
    """Execute a previously built artifact using the command its build recorded."""

    project = _project(getattr(args, "project", "."))
    root = project.root
    target_profile = getattr(args, "target", "host")
    selected_worker = select_execution_worker(
        args, project_root=root, target_profile=target_profile
    )
    try:
        project = discover_project(root)
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if project is None:
        raise CliFailure(
            "project.not_found", f"no {PROJECT_FILENAME} found from {root}"
        )
    try:
        project_index = require_lifecycle_project_index(
            project.root, project.definition.source_intelligence
        )
    except ProjectSourceIntelligenceError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    component = _component_argument(args, project)
    requested_entrypoint = getattr(args, "entrypoint", None)
    try:
        export = load_artifact_export(root, component)
    except ArtifactExportError as exc:
        built = available_exports(root)
        message = exc.message
        if exc.code == "artifact_export.not_built" and built:
            message += f"; built components: {', '.join(built)}"
        raise CliFailure(exc.code, message) from exc
    if export.library_product is not None:
        raise CliFailure(
            "run.library_not_executable",
            "this artifact is an importable library, not a process; "
            "consume its exact import surface",
        )
    if export.target_profile != target_profile:
        raise CliFailure(
            "artifact_export.target_profile_mismatch",
            f"built artifact targets {export.target_profile!r}, not "
            f"{target_profile!r}; rebuild for the requested target",
        )
    if export.worker_identity != selected_worker.worker.identity:
        raise CliFailure(
            "artifact_export.worker_mismatch",
            f"built artifact is bound to worker {export.worker_id!r}; select that "
            "exact worker or rebuild for the requested worker",
        )
    supplied = list(getattr(args, "arguments", None) or [])
    source = "supplied"
    if not supplied:
        # A bare run demonstrates the first declared acceptance case on every worker.
        supplied, source = _demonstration_arguments(
            root, export.component, requested_entrypoint
        )
    # An explicit pipeline model and accepted-source-only continuation both bind the
    # build authority. ``run`` deliberately exposes neither input, so replay their
    # retained values only for authority reconstruction.
    dispatch_args = Namespace(
        **{
            **vars(args),
            "model": export.model_selector,
            "from_accepted_source": export.accepted_source_only,
        }
    )
    dispatch_request = create_execution_dispatch_request(
        dispatch_args,
        project_root=root,
        component=export.component,
        selected=selected_worker,
        action=LifecycleDispatchAction.RUN,
        artifact_reference=export.artifact_reference,
        application_arguments=tuple(supplied),
        entrypoint=requested_entrypoint,
    )
    health = admit_execution_worker_health(
        args, selected_worker, dispatch_request.identity
    )
    if dispatch_request.authority_identity != export.authority_identity:
        raise CliFailure(
            "artifact_export.authority_stale",
            "the Component, specifications, Flavors, toolchain requirements, or "
            "project source index changed; rebuild before running",
        )
    if selected_worker.worker.kind in {
        ExecutionWorkerKind.COMMAND,
        ExecutionWorkerKind.SSH,
    }:
        try:
            dispatcher = (
                CommandExecutionDispatcher()
                if selected_worker.worker.kind is ExecutionWorkerKind.COMMAND
                else SshExecutionDispatcher(SshLifecycleRequestHandler())
            )
            dispatched, health_polls = dispatch_with_worker_health_poll(
                args,
                selected_worker,
                dispatch_request.identity,
                lambda: dispatcher.dispatch(
                    selected_worker.worker,
                    dispatch_request,
                    cwd=root,
                ),
            )
        except ExecutionDispatchAdapterError as exc:
            admit_execution_worker_health(
                args, selected_worker, dispatch_request.identity
            )
            raise CliFailure(exc.code, exc.message) from exc
        if dispatched.status is not DispatchResultStatus.PASSED:
            admit_execution_worker_health(
                args, selected_worker, dispatch_request.identity
            )
        if dispatched.stderr:
            sys.stderr.write(dispatched.stderr)
        response = {
            "schema": RUN_SCHEMA,
            "component": export.component,
            "target_profile": target_profile,
            "execution_worker": {
                "worker_id": selected_worker.worker.worker_id,
                "worker_identity": selected_worker.worker.identity.uri,
                "catalog_identity": selected_worker.catalog_identity.uri,
            },
            "artifact": export.artifact_reference.uri,
            "artifact_identity": export.artifact_reference.identity.uri,
            "argv": [],
            "arguments_source": source,
            "exit_status": dispatched.exit_status,
            "stdout": dispatched.stdout,
            "stderr": dispatched.stderr,
            "dispatch": dispatched.to_dict(),
            "project_source_intelligence": project_index,
        }
        if requested_entrypoint is not None:
            response["entrypoint"] = requested_entrypoint
        if health is not None:
            response["worker_health"] = health
            response["worker_health_polls"] = list(health_polls)
        return response, (0 if dispatched.status is DispatchResultStatus.PASSED else 1)
    if export.artifact is None:
        raise CliFailure(
            "artifact_export.invalid",
            "local execution requires a locally retained artifact",
        )

    selected_command = None
    if export.entrypoints:
        selected_name = requested_entrypoint or export.default_entrypoint
        selected_command = next(
            (item for item in export.entrypoints if item.name == selected_name), None
        )
        if selected_command is None:
            available = ", ".join(item.name for item in export.entrypoints)
            raise CliFailure(
                "artifact_export.entrypoint_unknown",
                f"unknown entrypoint {selected_name!r}; available entrypoints: "
                f"{available}",
            )
    elif requested_entrypoint is not None:
        raise CliFailure(
            "artifact_export.entrypoint_unsupported",
            "this single-entrypoint artifact has no named entrypoint selector; "
            "rebuild after declaring multiple entrypoints",
        )

    command_argv = export.argv if selected_command is None else selected_command.argv
    command_environment = (
        export.environment if selected_command is None else selected_command.environment
    )
    argv = [*command_argv, *supplied]
    run_environment = inherited_verbose_environment(
        {**os.environ, **command_environment}
    )
    command_cwd = export.artifact if export.entrypoints else export.artifact.parent
    trace_subprocess(argv, cwd=command_cwd, environment=run_environment)
    completed = subprocess.run(
        argv,
        cwd=str(command_cwd),
        env=run_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    trace_subprocess(
        argv,
        cwd=command_cwd,
        environment=run_environment,
        status=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    # The application's own output must never share the stream with the JSON envelope:
    # a caller parsing stdout would read the application's first document and choke on
    # the envelope behind it. On a terminal the raw output is what a human wants, so
    # write it there; otherwise carry it inside the envelope as data.
    interactive = False
    try:
        interactive = bool(sys.stdout.isatty())
    except (AttributeError, OSError):
        interactive = False
    if interactive and completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    response = {
        "schema": RUN_SCHEMA,
        "component": export.component,
        "target_profile": target_profile,
        "execution_worker": {
            "worker_id": selected_worker.worker.worker_id,
            "worker_identity": selected_worker.worker.identity.uri,
            "catalog_identity": selected_worker.catalog_identity.uri,
        },
        "artifact": str(export.artifact),
        "argv": list(argv),
        "arguments_source": source,
        "exit_status": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "project_source_intelligence": project_index,
    }
    if selected_command is not None:
        response["entrypoint"] = selected_command.name
        response["deployment_unit"] = selected_command.deployment_unit
    return response, (0 if completed.returncode == 0 else 1)


__all__ = [
    "BUILD_SCHEMA",
    "RUN_SCHEMA",
    "TEST_SCHEMA",
    "build_from_args",
    "run_from_args",
    "test_from_args",
]
