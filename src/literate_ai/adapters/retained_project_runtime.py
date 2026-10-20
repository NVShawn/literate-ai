"""Local retained orchestration through the existing live execution/admission owners.

This owner grants no regenerative, remote-worker, package or publication authority.
It preserves the private run on success and failure for operator inspection.
"""

from __future__ import annotations

from pathlib import Path

from literate_ai.adapters.retained_project_execution import (
    RetainedProjectExecutionError,
    RetainedProjectExecutionWorkspace,
)
from literate_ai.adapters.retained_project_receipt import admit_retained_project
from literate_ai.cache_directories import (
    bind_cache_directories,
    ensure_cache_directory,
)
from literate_ai.contracts import canonical_json_bytes
from literate_ai.contracts.execution_dispatch import ExecutionWorkerKind
from literate_ai.storage.cas import FileSystemCAS


def _retain_observations(workspace, store):
    """Keep actual command streams, including failures, without issuing a receipt."""
    observations = []
    for item in workspace.observations:
        stdout = store.put_bytes(item.result.stdout)
        stderr = store.put_bytes(item.result.stderr)
        observation = store.put_manifest(item.to_dict())
        observations.append(
            {
                "observation": observation.to_dict(),
                "stdout": stdout.to_dict(),
                "stderr": stderr.to_dict(),
            }
        )
    document = {
        "plan": workspace.plan.identity.uri,
        "command_observations": observations,
        "admission": False,
        "release_authorized": False,
    }
    # A diagnostic index is deliberately separate from the retained receipt.
    # Exclusive publication never overwrites an earlier record in a run.
    with (workspace.root / "command-diagnostics.json").open("xb") as stream:
        stream.write(canonical_json_bytes(document))


def execute_local_retained_project(
    source: Path,
    manifest,
    planning,
    *,
    component_locks,
    worker,
    current_plan,
    test_receipt_policy,
    cache_directories,
    authorization,
    allow_host_execution,
):
    """Run one reviewed local plan and return its live retained admission.

    The caller supplies current project/worker re-planning, not a cached plan.
    Existing workspace guards continue to require selected SDK producer owners;
    this local entry cannot substitute ambient SDK discovery for those bindings.
    """
    plan = planning.plan
    plan.require_authorization(authorization)
    if allow_host_execution is not True:
        raise RetainedProjectExecutionError("retained.execution.authorization-required")
    if worker.kind is not ExecutionWorkerKind.LOCAL:
        raise RetainedProjectExecutionError(
            "retained.execution.worker-runtime-unavailable"
        )
    if worker.identity != plan.profile.worker_identity:
        raise RetainedProjectExecutionError("retained.execution.worker-changed")
    if plan.profile.acceptance_policy_identity is None:
        raise RetainedProjectExecutionError(
            "retained.execution.independent-acceptance-required"
        )
    if current_plan() != plan:
        raise RetainedProjectExecutionError("retained.execution.plan-changed")
    directories = bind_cache_directories(
        source,
        build_dir=cache_directories.build_dir,
        obj_dir=cache_directories.obj_dir,
    )
    for path in (directories.build_dir, directories.obj_dir):
        if any(
            path == source / name or path.is_relative_to(source / name)
            for name in manifest.roots
        ):
            raise RetainedProjectExecutionError(
                "retained.execution.cache-overlaps-inputs"
            )
    runs = (
        ensure_cache_directory(
            directories.obj_dir,
            kind="object",
            project_root=source,
            required_subdirectories=(Path("retained-project-runs"),),
        )
        / "retained-project-runs"
    )
    custody = (
        ensure_cache_directory(
            directories.build_dir,
            kind="generated-source",
            project_root=source,
            required_subdirectories=(Path("retained-project-cas"),),
        )
        / "retained-project-cas"
    )
    store = FileSystemCAS(custody)
    workspace = RetainedProjectExecutionWorkspace(
        source,
        manifest,
        plan,
        parent=runs,
        current_plan=current_plan,
        authorization=authorization,
        allow_host_execution=allow_host_execution,
        test_receipt_policy=test_receipt_policy,
        managed_graph=planning.managed_sbom_graph,
        component_locks=component_locks,
    )
    try:
        for _ in plan.profile.actions:
            workspace.execute_next()
        admission = admit_retained_project(workspace, store)
        admission.require_current()
        return admission, workspace
    except Exception as exc:
        raise RetainedProjectExecutionError(
            f"{exc}; retained run: {workspace.root}", workspace.observations
        ) from exc
    finally:
        _retain_observations(workspace, store)
