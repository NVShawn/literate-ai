"""Reviewed native unit packaging inside the genuine retained lifecycle.

Historical receipts are absent from this execution boundary. Original lifecycle
owners issue fresh admission; packaging rechecks genuine SDK and LINK owners.
"""

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.component_lock_application import (
    current_project_component_locks,
)
from literate_ai.adapters.freebsd_packaging import FreeBsdPkgToolBinding
from literate_ai.adapters.native_tar_archives import NativeTarToolBinding
from literate_ai.adapters.retained_project_planning import RetainedProjectPlanningResult
from literate_ai.adapters.retained_project_runtime import (
    execute_local_retained_project,
    execute_receiver_retained_project,
)
from literate_ai.adapters.retained_project_unit_packages import (
    RetainedProjectUnitPackages,
)
from literate_ai.adapters.retained_unit_link import link_retained_unit_packages
from literate_ai.adapters.retained_unit_results import read_retained_unit_results
from literate_ai.adapters.retained_unit_specifications import (
    RetainedUnitSpecificationContext,
    bind_retained_unit_specifications,
)
from literate_ai.adapters.retained_unit_tool_observations import (
    retained_unit_tool_observations,
)
from literate_ai.adapters.retained_unit_verification import (
    RetainedUnitVerificationSelection,
)
from literate_ai.cache_directories import (
    ensure_cache_directory,
    select_cache_directories,
)
from literate_ai.contracts import (
    ContentIdentity,
    ExecutionWorkerKind,
    canonical_identity,
)
from literate_ai.contracts.retained_project import RetainedProjectManifest

from .execution_workers import (
    SelectedExecutionWorker,
    admit_execution_worker_health,
    dispatch_with_worker_health_poll,
)
from .retained_unit_package import _project_context

EXECUTION_PLAN_SCHEMA = "literate-ai/retained-unit-package-execution-plan@1"
EXECUTION_SCHEMA = "literate-ai/retained-unit-package-execution@1"


def _worker_selection_identity(selected):
    return canonical_identity(
        {
            "worker": selected.worker.identity.uri,
            "catalog": selected.catalog_identity.uri,
            "parameters": dict(selected.parameters),
        }
    )


@dataclass(frozen=True, slots=True)
class _PreparedRetainedUnitExecution:
    """Actual invocation-local planning objects, never decoded admission grants."""

    context: tuple
    manifest: RetainedProjectManifest
    selected: SelectedExecutionWorker
    planning: RetainedProjectPlanningResult
    specification_context: RetainedUnitSpecificationContext
    verification_selection: RetainedUnitVerificationSelection | None
    request: dict[str, object]
    authorization: ContentIdentity
    original_context: object

    def preview(self):
        return {
            "schema": "literate-ai/retained-unit-package-verification-plan@1"
            if self.verification_selection is not None
            else EXECUTION_PLAN_SCHEMA,
            "request": self.request,
            "authorization_identity": self.authorization.uri,
            "executed": False,
            "release_authorized": False,
        }


def _prepare_retained_unit_execution(
    args, *, receiver_context=None, verification_selection=None
):
    context = _project_context(args)
    project, binding, policy, observations, _pkg, _tar, authority = context
    manifest, selected, planning = authority()
    plan = planning.plan
    specification_context = bind_retained_unit_specifications(
        project,
        project.root,
        manifest,
        current_project_component_locks(project),
        maximum_units=policy.max_units,
        maximum_memory_bytes=policy.max_provider_memory_bytes,
    )
    verifying = args.retained_unit_command in {"verify-plan", "verify"}
    if verifying:
        from .retained_unit_verification import verification_selection_from_args

        if verification_selection is None:
            verification_selection = verification_selection_from_args(args, policy)
        verification_selection.require_current(policy)
    elif verification_selection is not None:
        raise ValueError("retained.unit.verification-operation-required")
    request = {
        "authority_mode": "retained-project",
        "operation": "verify" if verifying else "execute",
        "retained_plan_identity": plan.identity.uri,
        "worker_selection_identity": _worker_selection_identity(selected).uri,
        "policy_identity": policy.identity.uri,
        "native_tool_observations_identity": canonical_identity(observations).uri,
        "requires_host_execution_authorization": True,
        "release_authorized": False,
    }
    if verifying:
        request.update(verification_selection.to_dict())
    authorization = canonical_identity(request)
    original_context = None
    if (
        verifying
        and verification_selection.original_evidence is not None
        and receiver_context is None
        and (
            verification_selection.source is not None
            or args.retained_unit_command != "verify-plan"
        )
    ):
        # A receiver's read-only preview has intent assertions, not the
        # controller's filesystem custody. Actual receiver execution reopens
        # original metadata below under its live transport before any lifecycle.
        from .retained_unit_verification import original_context_for_verification

        original_context = original_context_for_verification(
            verification_selection,
            project=project,
            binding=binding,
            manifest=manifest,
            planning=planning,
            selected=selected,
            policy=policy,
            pkg_tool=_pkg,
            tar_tool=_tar,
            request=request,
            specification_context=specification_context,
        )
    return _PreparedRetainedUnitExecution(
        context,
        manifest,
        selected,
        planning,
        specification_context,
        verification_selection,
        request,
        authorization,
        original_context,
    )


def execute_retained_unit_package_from_args(
    args, *, receiver_context=None, verification_selection=None
):
    prepared = _prepare_retained_unit_execution(
        args,
        receiver_context=receiver_context,
        verification_selection=verification_selection,
    )
    project, binding, policy, observations, _pkg, _tar, authority = prepared.context
    manifest, selected, planning = (
        prepared.manifest,
        prepared.selected,
        prepared.planning,
    )
    plan = planning.plan
    specification_context = prepared.specification_context
    verification_selection = prepared.verification_selection
    verifying = verification_selection is not None
    request, authorization = prepared.request, prepared.authorization
    original_context = prepared.original_context
    if args.retained_unit_command in {"execution-plan", "verify-plan"}:
        return prepared.preview(), 0
    plan.require_authorization(args.authorize_retained_project)
    if args.authorize_unit_packages != authorization.uri:
        raise ValueError("retained.unit.exact-package-authorization-required")
    if args.allow_host_execution is not True:
        raise ValueError("retained.execution.authorization-required")
    if receiver_context is None:
        if selected.worker.kind is not ExecutionWorkerKind.LOCAL:
            from .retained_unit_remote_execution import (
                execute_remote_retained_unit_package,
            )

            return execute_remote_retained_unit_package(
                args,
                project=project,
                binding=binding,
                policy=policy,
                observations=observations,
                pkg_tool=_pkg,
                tar_tool=_tar,
                manifest=manifest,
                selected=selected,
                planning=planning,
                execution_request=request,
                verification_selection=verification_selection,
            )
    else:
        from literate_ai.adapters.retained_receiver_context import (
            RetainedReceiverContext,
        )

        if not isinstance(receiver_context, RetainedReceiverContext):
            raise TypeError("retained.receiver.live-measured-context-required")
        receiver_context.require_plan(plan)
        if (
            selected.worker != receiver_context.worker
            or selected.catalog_identity != receiver_context.catalog_identity
        ):
            raise ValueError("retained.unit.execution-worker-selection-changed")
    if platform.system().casefold() != "freebsd":
        raise ValueError("retained.unit.native-execution-requires-freebsd")
    # These bindings come from the actual receiver, never the preview decoder.
    pkg_tool, tar_tool = (
        FreeBsdPkgToolBinding.discover(),
        NativeTarToolBinding.discover(),
    )
    if retained_unit_tool_observations(pkg_tool, tar_tool) != observations:
        raise ValueError("retained.unit.current-native-tools-differ")

    def current_authority_plan():
        if verifying:
            verification_selection.require_current(policy)
        _manifest, current_worker, current = authority()
        if _worker_selection_identity(current_worker) != _worker_selection_identity(
            selected
        ):
            raise ValueError("retained.unit.execution-worker-selection-changed")
        return current.plan

    def current_plan():
        if receiver_context is not None:
            receiver_context.require_plan(plan)
        return current_authority_plan()

    if (
        receiver_context is not None
        and type(receiver_context) is RetainedReceiverContext
    ):
        from literate_ai.adapters.retained_project_platform import (
            _compose_receiver_current_plan,
        )

        runtime_current_plan = _compose_receiver_current_plan(
            receiver_context, plan, current_authority_plan
        )
    else:
        runtime_current_plan = current_plan

    if current_plan() != plan:
        raise ValueError("retained.unit.execution-authority-changed")
    # The receiver observes this host directly through its live context. Health
    # dispatch/polling belongs to the controller; recursively SSHing to this same
    # worker from inside its job would invent another execution boundary.
    health = (
        admit_execution_worker_health(args, selected, authorization)
        if receiver_context is None
        else None
    )
    directories = select_cache_directories(
        project.root, build_dir=args.build_dir, obj_dir=args.obj_dir
    )
    work = (
        ensure_cache_directory(
            directories.obj_dir,
            kind="object",
            project_root=project.root,
            required_subdirectories=(Path("retained-unit-packages"),),
        )
        / "retained-unit-packages"
    )
    if (
        verifying
        and verification_selection.original_evidence is not None
        and receiver_context is not None
    ):
        from literate_ai.storage.cas import FileSystemCAS

        from .retained_unit_verification import original_context_for_verification

        custody = (
            ensure_cache_directory(
                directories.build_dir,
                kind="generated-source",
                project_root=project.root,
                required_subdirectories=(Path("retained-project-cas"),),
            )
            / "retained-project-cas"
        )
        original_context = original_context_for_verification(
            verification_selection,
            project=project,
            binding=binding,
            manifest=manifest,
            planning=planning,
            selected=selected,
            policy=policy,
            pkg_tool=pkg_tool,
            tar_tool=tar_tool,
            request=request,
            specification_context=specification_context,
            receiver_context=receiver_context,
            object_root=work,
            store=FileSystemCAS(custody),
        )
    completed = []
    original_evidence = []
    verified_projections = []

    def consume(admission, workspace):
        if current_plan() != plan:
            raise ValueError("retained.unit.execution-authority-changed")
        owner = RetainedProjectUnitPackages(
            admission,
            pkg_tool=pkg_tool,
            tar_tool=tar_tool,
            specification_context=specification_context,
            **policy.owner_arguments(),
        )
        evidence = owner.retain_planning_evidence()
        handoff = link_retained_unit_packages(owner)
        run_reference = None
        if (
            receiver_context is not None
            or original_context is not None
            or getattr(args, "verification_evidence_output", None) is not None
        ):
            from literate_ai.adapters.retained_unit_run_evidence import (
                retain_retained_unit_run_evidence,
            )

            run_reference = retain_retained_unit_run_evidence(
                admission,
                maximum_bytes=policy.max_provider_metadata_bytes,
                maximum_memory_bytes=policy.max_provider_memory_bytes,
                admission_guard=(
                    admission.require_current
                    if receiver_context is None
                    else receiver_context.require_current
                ),
                metadata_poll_guard=(
                    receiver_context.require_custody_current
                    if type(receiver_context) is RetainedReceiverContext
                    else None
                ),
            )
            handoff.require_current(owner)
        fresh_workspace_roots = ()
        if original_context is not None:
            from literate_ai.adapters.retained_original_unit_context import (
                reopen_fresh_unit_workspace_roots,
            )
            from literate_ai.adapters.retained_sdk_authority import (
                current_retained_sdk_snapshots,
            )

            locks = current_project_component_locks(project)
            snapshots = (
                current_retained_sdk_snapshots(project, locks)
                if any(m.role == "sdk-policy" for m in manifest.members)
                else ()
            )
            fresh_workspace_roots = reopen_fresh_unit_workspace_roots(
                original_context,
                owner,
                planning,
                policy,
                run_reference,
                sdk_snapshots=snapshots,
                guard=(
                    admission.require_current
                    if receiver_context is None
                    else receiver_context.require_custody_current
                ),
            )
        if verifying:
            from .retained_unit_verification import receive_selected_unit_packages

            reference = receive_selected_unit_packages(
                verification_selection,
                owner,
                object_root=work,
                receiver_context=receiver_context,
                original_context=original_context,
            )
            # The owner reopens these exact bytes under genuine fresh admission.
            # A different projection is refused by the canonical result reader.
            if original_context is None:
                owner.verify_set(reference, object_root=work)
            else:
                owner.verify_original_set(
                    original_context,
                    object_root=work,
                    link_handoff=handoff,
                    fresh_workspace_roots=fresh_workspace_roots,
                )
            verified_projections.append(
                owner.projection
                if original_context is None
                else original_context.projection
            )
        else:
            reference = owner.construct_set(object_root=work, link_handoff=handoff)
        package_projection = (
            owner.projection
            if original_context is None
            else original_context.projection
        )
        index, _results = read_retained_unit_results(
            package_projection, reference, admission.store, **owner.tree_policy
        )
        handoff.require_current(owner)
        request_reference = admission.store.put_manifest(
            request, media_type="application/json"
        )
        if request_reference.identity != authorization.uri:
            raise ValueError("retained.unit.execution-request-retention-differs")
        completed.append(
            {
                "schema": "literate-ai/retained-unit-package-verification@1"
                if verifying
                else EXECUTION_SCHEMA,
                "authority_mode": "retained-project",
                "authorization_identity": authorization.uri,
                "execution_request": request_reference.to_dict(),
                "retained_plan_identity": plan.identity.uri,
                "retained_receipt_identity": admission.receipt.identity.uri,
                "planning_evidence": evidence.to_dict(),
                "link_proof": handoff.reference.to_dict(),
                "set_plan_identity": package_projection.set_plan.identity.uri,
                "result_index": reference.to_dict(),
                "archive_bytes": index.archive_bytes,
                "unit_count": index.unit_count,
                "custody_root": str(admission.store.root),
                "workspace": str(workspace.root),
                "admitted": True,
                "native_verified": True,
                "release_authorized": False,
            }
        )
        if verifying:
            completed[-1].update(
                operation="verify", verified_result_index=reference.to_dict()
            )
        if run_reference is not None:
            completed[-1]["accepted_run_evidence"] = run_reference.to_dict()
        if original_context is not None:
            from literate_ai.adapters.retained_original_unit_context import (
                original_unit_verification_proof,
            )
            from literate_ai.adapters.retained_unit_inspection import (
                retained_unit_metadata_reference,
            )
            from literate_ai.contracts.blobs import BlobRef

            proof = original_unit_verification_proof(
                original_context,
                owner.projection,
                fresh_completed_run=admission.run,
                fresh_inputs=owner.inputs,
                fresh_workspace_roots=fresh_workspace_roots,
                fresh_receipt=retained_unit_metadata_reference(
                    admission.store,
                    admission.receipt.identity,
                    maximum_bytes=min(64 * 1024**2, policy.max_provider_metadata_bytes),
                ),
                fresh_planning=evidence,
                fresh_run=BlobRef.from_dict(completed[-1]["accepted_run_evidence"]),
                fresh_link=handoff.reference,
                result_index=reference,
                results=_results,
            )
            completed[-1]["original_verification_proof"] = admission.store.put_manifest(
                proof, media_type="application/json"
            ).to_dict()
            handoff.require_current(owner)
        if getattr(args, "verification_evidence_output", None) is not None:
            from literate_ai.adapters.retained_original_unit_evidence import (
                OriginalRetainedUnitEvidence,
            )
            from literate_ai.adapters.retained_unit_inspection import (
                retained_unit_metadata_reference,
            )
            from literate_ai.contracts.blobs import BlobRef

            original_evidence.append(
                OriginalRetainedUnitEvidence(
                    plan.identity,
                    selected.worker.identity,
                    request_reference,
                    retained_unit_metadata_reference(
                        admission.store,
                        admission.receipt.identity,
                        maximum_bytes=min(
                            64 * 1024**2, policy.max_provider_metadata_bytes
                        ),
                    ),
                    evidence,
                    BlobRef.from_dict(completed[-1]["accepted_run_evidence"]),
                    handoff.reference,
                    reference,
                )
            )

    def execute():
        runtime = (
            execute_local_retained_project
            if receiver_context is None
            else execute_receiver_retained_project
        )
        return runtime(
            project.root,
            manifest,
            planning,
            component_locks=current_project_component_locks(project),
            **(
                {"worker": selected.worker}
                if receiver_context is None
                else {"receiver_context": receiver_context}
            ),
            current_plan=runtime_current_plan,
            test_receipt_policy=project.definition.test_receipt_policy,
            cache_directories=directories,
            authorization=args.authorize_retained_project,
            allow_host_execution=args.allow_host_execution,
            operator_environment=dict(os.environ),
            sdk_project=project,
            sdk_binding=binding,
            consume_admission=consume,
        )

    if receiver_context is None:
        (admission, _workspace), polls = dispatch_with_worker_health_poll(
            args, selected, authorization, execute
        )
    else:
        admission, _workspace = execute()
        polls = ()
    if len(completed) != 1 or current_plan() != plan:
        raise ValueError("retained.unit.complete-current-execution-required")
    if verifying and verification_selection.source is not None:
        if len(verified_projections) != 1:
            raise ValueError("retained.unit.complete-original-verification-required")
        verification_selection.require_original_source(
            verified_projections[0],
            policy,
            admission_guard=verification_selection.source.require_current,
        )
    admission.require_current()
    pkg_tool.require_unchanged()
    tar_tool.require_current()
    if getattr(args, "verification_evidence_output", None) is not None:
        from .retained_unit_verification import write_original_verification_evidence

        if len(original_evidence) != 1:
            raise ValueError("retained.unit.complete-original-evidence-required")
        original_evidence[0].require_budget(policy)
        write_original_verification_evidence(
            args.verification_evidence_output, original_evidence[0]
        )
    return {
        **completed[0],
        "worker_health": health,
        "worker_health_polls": list(polls),
    }, 0
