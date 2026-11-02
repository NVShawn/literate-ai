"""Private command-worker composition for the public retained PACKAGE operation."""

import os
import secrets
from pathlib import Path

from literate_ai.adapters.action_command_dispatch import (
    CommandLifecycleActionDispatcher,
)
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_execution_config import load_action_execution
from literate_ai.adapters.action_retained_unit_package_worker import (
    retained_unit_package_action,
    retained_unit_package_input,
    retained_unit_verification_input,
)
from literate_ai.adapters.component_lock_application import (
    current_project_component_locks,
)
from literate_ai.adapters.retained_sdk_authority import current_retained_sdk_snapshots
from literate_ai.adapters.retained_unit_controller import (
    execute_and_import_retained_unit_package,
)
from literate_ai.adapters.user_assets import WORKER_CONFIG_ENVIRONMENT
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.cache_directories import (
    ensure_cache_directory,
    select_cache_directories,
)
from literate_ai.contracts import canonical_identity

from .errors import CliFailure
from .execution_workers import (
    admit_execution_worker_health,
    dispatch_with_worker_health_poll,
)


def execute_remote_retained_unit_package(
    args,
    *,
    project,
    binding,
    policy,
    observations,
    pkg_tool,
    tar_tool,
    manifest,
    selected,
    planning,
    execution_request,
    verification_selection=None,
):
    """Use current private worker configuration, never transport paths in intent."""
    from .retained_unit_execution import _worker_selection_identity
    from .retained_unit_package import _project_context

    authorization = canonical_identity(execution_request)
    if selected.configuration is None:
        raise ValueError("retained.unit.explicit-command-worker-required")
    bound = load_action_execution(
        project_root=project.root,
        environment={
            **os.environ,
            WORKER_CONFIG_ENVIRONMENT: str(selected.configuration),
        },
    )
    if bound is None:
        raise CliFailure(
            "retained_unit_package.remote_configuration_required",
            "remote retained packages require private action execution configuration",
        )

    def source_current():
        if verification_selection is not None:
            verification_selection.require_current(policy)
        bound.require_unchanged()
        binding.require_unchanged()
        (
            fresh_project,
            fresh_binding,
            fresh_policy,
            fresh_observations,
            _pkg,
            _tar,
            authority,
        ) = _project_context(args)
        fresh_manifest, fresh_selected, fresh = authority()
        if (
            fresh_project.root != project.root
            or fresh_manifest != manifest
            or fresh.plan != planning.plan
            or fresh_policy != policy
            or fresh_observations != observations
            or _worker_selection_identity(fresh_selected)
            != _worker_selection_identity(selected)
            or canonical_identity(fresh_binding.trust_binding.to_dict())
            != canonical_identity(binding.trust_binding.to_dict())
        ):
            raise ValueError("retained.unit.execution-authority-changed")

    source_current()
    pool, store = bound.admit(
        project_root=project.root,
        target_profile=selected.worker.target_profile,
        job_identity=authorization,
        phase=LifecycleActionKind.PACKAGE,
    )
    matches = [
        worker
        for worker in pool.workers
        if worker.worker_id == selected.worker.worker_id
        and worker.worker_identity == selected.worker.identity
        and worker.catalog_identity == selected.catalog_identity
    ]
    if len(matches) != 1:
        raise ValueError("retained.unit.selected-command-worker-not-admitted")
    input_record = (
        retained_unit_package_input(execution_request)
        if verification_selection is None
        else retained_unit_verification_input(execution_request)
    )
    input_identity = record_identity(input_record)
    request = LifecycleActionDispatchRequest(
        canonical_identity(
            {
                "schema": "literate-ai/retained-unit-package-dispatch@1",
                "authorization": authorization.uri,
                "pool": pool.identity.uri,
                "nonce": secrets.token_hex(16),
            }
        ),
        retained_unit_package_action(
            planning.plan.identity, selected.worker.worker_id, input_identity
        ),
        matches[0],
        0,
        (),
        pool.deadline.identity,
        (input_identity,),
    )

    def retain(identity, content):
        if (
            record_identity(content) != identity
            or store.put_bytes(content, media_type="application/json").identity
            != identity.uri
        ):
            raise ValueError("retained.unit.dispatch-record-custody-differs")

    def inputs(current):
        if current != request:
            raise ValueError("retained.unit.dispatch-input-selection-changed")
        return {input_identity: input_record}

    def unknown(current):
        # Correlation is retained without asserting that transport termination
        # stopped the native job. The receiver's native attempt fence owns reuse.
        document = {
            "schema": "literate-ai/retained-unit-unknown-ownership@1",
            "request_identity": current.identity.uri,
            "worker_identity": current.worker.worker_identity.uri,
            "native_termination_verified": False,
            "release_authorized": False,
        }
        store.put_manifest(document, media_type="application/json")

    dispatcher = CommandLifecycleActionDispatcher(
        pool.catalog,
        pool.workers,
        pool.deadline,
        cwd=project.root,
        input_records=inputs,
        record_result=retain,
        record_failure=retain,
        record_attested_failure=retain,
        on_unknown_ownership=unknown,
        revalidate_worker=pool.revalidate,
        environment=bound.environment,
        attested_boundary=pool.attested_boundary,
    )
    directories = select_cache_directories(
        project.root, build_dir=args.build_dir, obj_dir=args.obj_dir
    )
    objects = (
        ensure_cache_directory(
            directories.obj_dir,
            kind="object",
            project_root=project.root,
            required_subdirectories=(Path("retained-unit-returns"),),
        )
        / "retained-unit-returns"
    )
    locks = current_project_component_locks(project)

    def snapshots():
        return (
            current_retained_sdk_snapshots(project, locks)
            if any(member.role == "sdk-policy" for member in manifest.members)
            else ()
        )

    health = admit_execution_worker_health(args, selected, authorization)

    def execute():
        return execute_and_import_retained_unit_package(
            dispatcher,
            pool,
            request,
            execution_request=execution_request,
            source=project.root,
            manifest=manifest,
            planning=planning,
            component_locks=locks,
            policy=policy,
            pkg_tool=pkg_tool,
            tar_tool=tar_tool,
            store=store,
            object_root=objects,
            test_receipt_policy=project.definition.test_receipt_policy,
            source_guard=source_current,
            sdk_snapshots=snapshots,
            verification_selection=verification_selection,
        )

    admitted, polls = dispatch_with_worker_health_poll(
        args, selected, authorization, execute
    )
    result = admitted.to_dict()
    return {
        **result,
        "custody_root": str(store.root),
        "worker_health": health,
        "worker_health_polls": list(polls),
    }, 0
