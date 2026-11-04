"""Retained orchestration through the existing live execution/admission owners.

Local execution or a live attested receiver context selects the execution boundary.
This owner grants no regenerative, package or publication authority.
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
from literate_ai.adapters.retained_project_platform import (
    RetainedLocalPlatform,
    RetainedReceiverPlatform,
)
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
    consume_admission=None,
):
    """Run one reviewed local plan and return its live retained admission.

    The caller supplies current project/worker re-planning, not a cached plan.
    Existing workspace guards continue to require selected SDK producer owners;
    this local entry cannot substitute ambient SDK discovery for those bindings.
    """
    return _execute_retained_project(
        source,
        manifest,
        planning,
        component_locks=component_locks,
        worker=worker,
        current_plan=current_plan,
        test_receipt_policy=test_receipt_policy,
        cache_directories=cache_directories,
        authorization=authorization,
        allow_host_execution=allow_host_execution,
        operator_environment=operator_environment,
        sdk_project=sdk_project,
        sdk_binding=sdk_binding,
        consume_admission=consume_admission,
    )


def execute_receiver_retained_project(
    source, manifest, planning, *, receiver_context, **options
):
    """Execute on the observed receiver while retaining its original SSH identity.

    The private receiver registration owns options and current re-planning. A
    parsed receipt or a LOCAL replacement worker cannot select this entry.
    """
    from literate_ai.adapters.retained_receiver_context import RetainedReceiverContext

    if not isinstance(receiver_context, RetainedReceiverContext):
        raise TypeError("retained.receiver.live-measured-context-required")
    receiver_context.require_plan(planning.plan)
    return _execute_retained_project(
        source,
        manifest,
        planning,
        worker=receiver_context.worker,
        receiver_context=receiver_context,
        **options,
    )


def _execute_retained_project(
    source,
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
    consume_admission=None,
    receiver_context=None,
):
    plan = planning.plan
    if consume_admission is not None and not callable(consume_admission):
        raise TypeError("retained.execution.admission-consumer-required")
    plan.require_authorization(authorization)
    if allow_host_execution is not True:
        raise RetainedProjectExecutionError("retained.execution.authorization-required")
    if receiver_context is None and worker.kind is not ExecutionWorkerKind.LOCAL:
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
    platform = (
        RetainedLocalPlatform(worker, component_locks, plan)
        if receiver_context is None
        else RetainedReceiverPlatform(receiver_context, component_locks, plan)
    )
    platform.require_current()

    def current_local_plan():
        if receiver_context is not None:
            return platform.current_plan(current_plan)
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
                receiver_context=receiver_context,
            )
            sdk_arguments = {
                "native_sdk_owners": sdk_runtime.acquire(),
                "native_sdk_authorize": sdk_runtime.authorize,
                "native_sdk_record": sdk_runtime.record,
                "native_sdk_record_many": sdk_runtime.record_many,
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
        receiver_context=receiver_context,
        **sdk_arguments,
    )
    try:
        for _ in plan.profile.actions:
            workspace.execute_next()
        admission = admit_retained_project(
            workspace, store, worker_evidence=platform.evidence()
        )
        admission.require_current()
        if consume_admission is not None:
            # Framework composition supplies this callable, never serialized
            # input. Secondary views were finalized by admission; consumers use
            # sealed CAS payloads and revalidate genuine SDK/package owners.
            consume_admission(admission, workspace)
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
