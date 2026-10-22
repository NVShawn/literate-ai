"""Local retained orchestration through the existing live execution/admission owners.

This owner grants no regenerative, remote-worker, package or publication authority.
It preserves the private run on success and failure for operator inspection.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.retained_allocation import RetainedAllocationOwner
from literate_ai.adapters.retained_project_execution import (
    RetainedProjectExecutionError,
    RetainedProjectExecutionWorkspace,
)
from literate_ai.adapters.retained_project_platform import RetainedLocalPlatform
from literate_ai.adapters.retained_project_receipt import admit_retained_project
from literate_ai.adapters.user_paths import resolve_user_paths
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
        **(
            {
                "secondary_access": {
                    "view_observations": list(workspace.secondary_access_evidence()),
                    "allocation_observations": list(
                        workspace._secondary_views.allocation.evidence()
                    ),
                }
            }
            if workspace.plan.profile.secondary_access is not None
            else {}
        ),
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
    operator_environment=None,
    sdk_project=None,
    sdk_binding=None,
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
    if (
        worker.identity != plan.profile.worker_identity
        or worker.target_profile != plan.profile.target
    ):
        raise RetainedProjectExecutionError("retained.execution.worker-changed")
    if plan.profile.acceptance_policy_identity is None:
        raise RetainedProjectExecutionError(
            "retained.execution.independent-acceptance-required"
        )
    if current_plan() != plan:
        raise RetainedProjectExecutionError("retained.execution.plan-changed")
    allocation = None
    if plan.profile.secondary_access is not None:
        if operator_environment is None:
            raise RetainedProjectExecutionError(
                "retained.secondary.provisioning-owner-required"
            )
        operator_environment = dict(operator_environment)
        private = resolve_user_paths(environment=operator_environment)
        allocation = RetainedAllocationOwner(
            Path(private.worker_provisioner),
            plan=plan,
            environment=operator_environment,
            minimum_remaining_seconds=60,
        )
        prefix = allocation.workspace_prefix
        if prefix.resolve(strict=True) != prefix or any(
            prefix == source / name or prefix.is_relative_to(source / name)
            for name in manifest.roots
        ):
            raise RetainedProjectExecutionError(
                "retained.secondary.allocated-parent-overlaps-or-redirects"
            )
        require_safe_directory(prefix)
    platform = RetainedLocalPlatform(worker, component_locks, plan)
    platform.require_current()

    def current_local_plan():
        platform.require_current()
        return current_plan()

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
    if allocation is None:
        runs = (
            ensure_cache_directory(
                directories.obj_dir,
                kind="object",
                project_root=source,
                required_subdirectories=(Path("retained-project-runs"),),
            )
            / "retained-project-runs"
        )
    else:
        allocation.require_plan(plan, minimum_remaining_seconds=60)
        runs = Path(
            tempfile.mkdtemp(
                prefix="retained-session-", dir=allocation.workspace_prefix
            )
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
    sdk_arguments = {}
    if any(member.role in {"sdk", "sdk-policy"} for member in manifest.members) or any(
        node.revision.repository_sources
        for lock in component_locks
        for node in lock.nodes
    ):
        from literate_ai.adapters.retained_project_sdk_runtime import (
            RetainedProjectSdkRuntime,
        )
        from literate_ai.application.generation_preparation import (
            GenerationPreparationError,
        )
        from literate_ai.security import AuthorizationError

        if sdk_project is None or sdk_binding is None:
            raise RetainedProjectExecutionError(
                "retained.sdk.producer-bindings-required"
            )
        if sdk_project.root != source:
            raise RetainedProjectExecutionError(
                "retained.sdk.project-authority-required"
            )
        if operator_environment is None:
            raise RetainedProjectExecutionError(
                "retained.sdk.operator-environment-required"
            )
        try:
            sdk_runtime = RetainedProjectSdkRuntime(
                sdk_project,
                sdk_binding,
                manifest,
                plan,
                component_locks=component_locks,
                current_plan=current_local_plan,
                cache_root=directories.build_dir / "native-sdk-cache",
                store=store,
                environment=operator_environment,
            )
            sdk_arguments = {
                "native_sdk_owners": sdk_runtime.acquire(),
                "native_sdk_authorize": sdk_runtime.authorize,
                "native_sdk_record": sdk_runtime.record,
            }
        except (
            AuthorizationError,
            GenerationPreparationError,
            ValueError,
            OSError,
        ) as exc:
            store.put_manifest(
                {
                    "schema": "literate-ai/retained-sdk-acquisition-diagnostic@1",
                    "plan_identity": plan.identity.uri,
                    "error_class": type(exc).__name__,
                    "admitted": False,
                }
            )
            raise RetainedProjectExecutionError(
                f"{exc}; retained SDK custody: {store.root}"
            ) from exc
    workspace = RetainedProjectExecutionWorkspace(
        source,
        manifest,
        plan,
        parent=runs,
        current_plan=current_local_plan,
        authorization=authorization,
        allow_host_execution=allow_host_execution,
        test_receipt_policy=test_receipt_policy,
        managed_graph=planning.managed_sbom_graph,
        component_locks=component_locks,
        allocation_owner=allocation,
        **sdk_arguments,
    )
    try:
        for _ in plan.profile.actions:
            workspace.execute_next()
        admission = admit_retained_project(
            workspace, store, worker_evidence=platform.evidence()
        )
        admission.require_current()
        return admission, workspace
    except Exception as exc:
        raise RetainedProjectExecutionError(
            f"{exc}; retained run: {workspace.root}", workspace.observations
        ) from exc
    finally:
        try:
            workspace.close_secondary_access()
        finally:
            _retain_observations(workspace, store)
