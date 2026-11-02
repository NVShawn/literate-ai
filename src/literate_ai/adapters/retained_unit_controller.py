"""Actuate and admit one retained PACKAGE through the real attested dispatcher.

Historical facts are reopened without restoring a local retained/SDK/LINK owner.
The only constructor for the returned operation capability follows actual worker
execution, exact current intent, complete portable archive verification and final
worker/source admission. Decoded result documents alone cannot construct it.
"""

import json
from pathlib import Path

from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_command_blob_source import CommandActionBlobSource
from literate_ai.adapters.action_command_dispatch import (
    CommandLifecycleActionDispatcher,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_retained_unit_package_worker import (
    RESULT_SCHEMA,
    VERIFICATION_RESULT_SCHEMA,
    retained_unit_package_action,
    retained_unit_package_input,
    retained_unit_verification_input,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_project_unit_packages import (
    plan_retained_unit_packages,
)
from literate_ai.adapters.retained_unit_inspection import (
    read_retained_unit_planning_inputs,
)
from literate_ai.adapters.retained_unit_link import retained_unit_link_document
from literate_ai.adapters.retained_unit_run_inspection import reopen_retained_unit_run
from literate_ai.adapters.retained_unit_transfer import (
    RetainedUnitReturnCustody,
    receive_retained_unit_package_set,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.execution_dispatch import ExecutionWorkerKind
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy
from literate_ai.storage.cas import FileSystemCAS

_ADMITTED_REMOTE_SET = object()
_EXECUTION_FIELDS = frozenset(
    {
        "authorization_identity",
        "execution_request",
        "retained_plan_identity",
        "retained_receipt_identity",
        "retained_receipt",
        "planning_evidence",
        "accepted_run_evidence",
        "link_proof",
        "set_plan_identity",
        "result_index",
        "archive_bytes",
        "unit_count",
    }
)


def _require(value):
    if not value:
        raise ActionWireError(
            "retained_unit_controller.context_mismatch",
            "retained PACKAGE return differs from its admitted operation",
        )


def _document(store, reference, maximum):
    _require(
        isinstance(reference, BlobRef)
        and reference.media_type == "application/json"
        and 0 < reference.size <= maximum
    )
    raw = store.get_bytes(reference)
    value = json.loads(raw)
    _require(type(value) is dict and canonical_json_bytes(value) == raw)
    return value


class AdmittedRemoteRetainedUnitPackageSet:
    """An exact operation's verified package set, never a decoder for live SDKs."""

    def __init__(
        self,
        request,
        inputs,
        run,
        projection,
        index,
        results,
        references,
        metrics,
        store,
        guard,
        *,
        verification_selection=None,
        _seal=None,
    ):
        if _seal is not _ADMITTED_REMOTE_SET:
            raise TypeError("retained.unit.actual-controller-admission-required")
        self.request, self.inputs, self.run = request, inputs, run
        self.verification_selection = verification_selection
        self.projection, self.index, self.results = projection, index, results
        self.references, self.metrics, self.store, self._guard = (
            references,
            metrics,
            store,
            guard,
        )

    def require_current(self):
        self._guard()
        for reference in self.references:
            self.store.verify(reference)
        for result in self.results:
            self.store.verify(result.archive)
        self._guard()

    def to_dict(self):
        self.require_current()
        result = {
            "schema": "literate-ai/retained-unit-remote-package-admission@1",
            "authority_mode": "retained-project",
            "verification_scope": "authenticated-worker-native-package",
            "request_identity": self.request.identity.uri,
            "worker_identity": self.request.worker.worker_identity.uri,
            "retained_plan_identity": self.inputs.plan.identity.uri,
            "retained_receipt_identity": self.inputs.receipt.identity.uri,
            "set_plan_identity": self.projection.set_plan.identity.uri,
            "unit_count": self.index.unit_count,
            "archive_bytes": self.index.archive_bytes,
            "result_index": self.references[-1].to_dict(),
            "transfer": {
                key: value
                for key, value in self.metrics.items()
                if key not in {"admitted", "native_verified", "release_authorized"}
            },
            "admitted": True,
            "native_verified": True,
            "release_authorized": False,
        }
        if self.verification_selection is not None:
            result.update(
                schema="literate-ai/retained-unit-remote-verification-admission@1",
                operation="verify",
                verification_scope="authenticated-worker-native-original-archive-verification",
                verified_result_index=self.verification_selection.result.to_dict(),
            )
        return result


def execute_and_import_retained_unit_package(
    dispatcher,
    pool,
    request,
    *,
    execution_request,
    source,
    manifest,
    planning,
    component_locks,
    policy,
    pkg_tool,
    tar_tool,
    store,
    object_root,
    test_receipt_policy,
    source_guard,
    sdk_snapshots=lambda: (),
    cancelled=lambda: False,
    verification_selection=None,
):
    """Dispatch through live worker admission and import only its exact result.

    Source authority is checked before/after this operation; inner guards check
    route/registration/hardware freshness, deadlines and cancellation. Return CAS
    readers themselves bind the private receiver registration on every transfer.
    """
    if (
        not isinstance(dispatcher, CommandLifecycleActionDispatcher)
        or not isinstance(pool, CommandActionWorkerPool)
        or not isinstance(request, LifecycleActionDispatchRequest)
        or not isinstance(policy, RetainedUnitPackagePolicy)
        or not isinstance(store, FileSystemCAS)
        or not all(
            callable(value) for value in (source_guard, sdk_snapshots, cancelled)
        )
    ):
        raise TypeError("retained.unit.live-controller-context-required")
    _require(
        dispatcher.catalog == pool.catalog
        and dispatcher.deadline == pool.deadline
        and request.worker in pool.workers
        and pool.phase is LifecycleActionKind.PACKAGE
        and getattr(dispatcher.attested_boundary, "__self__", None) is pool
        and getattr(dispatcher.revalidate_worker, "__self__", None) is pool
    )
    worker = pool.catalog.worker(request.worker.worker_id)
    plan = planning.plan
    if verification_selection is not None:
        from literate_ai.adapters.retained_unit_verification import (
            RetainedUnitVerificationSelection,
        )

        if (
            not isinstance(verification_selection, RetainedUnitVerificationSelection)
            or verification_selection.source is None
        ):
            raise TypeError("retained.unit.explicit-verification-selection-required")
        verification_selection.require_current(policy)
        _require(
            all(
                execution_request.get(name) == value
                for name, value in verification_selection.to_dict().items()
            )
        )
    intent = (
        retained_unit_package_input(execution_request)
        if verification_selection is None
        else retained_unit_verification_input(execution_request)
    )
    input_identity = record_identity(intent)
    authorization = canonical_identity(execution_request)
    _require(
        worker.kind is ExecutionWorkerKind.SSH
        and worker.identity
        == plan.profile.worker_identity
        == request.worker.worker_identity
        and request.worker.catalog_identity == pool.catalog.identity
        and request.deadline_identity == dispatcher.deadline.identity
        and request.action
        == retained_unit_package_action(plan.identity, worker.worker_id, input_identity)
        and not request.predecessor_result_identities
        and request.input_record_identities == (input_identity,)
        and execution_request["retained_plan_identity"] == plan.identity.uri
        and execution_request["authority_mode"] == "retained-project"
        and execution_request["operation"]
        == ("execute" if verification_selection is None else "verify")
        and execution_request["policy_identity"] == policy.identity.uri
        and execution_request["requires_host_execution_authorization"] is True
        and execution_request["release_authorized"] is False
    )
    hardware = pool.hardware_observations.worker(worker.worker_id)
    _require(hardware.os_family == "freebsd")
    parent = Path(object_root).absolute()
    directory_node(parent)

    def current():
        dispatcher.validate_request_state(request)
        if cancelled():
            raise ActionWireError(
                "retained_unit_controller.cancelled",
                "retained PACKAGE import was cancelled",
            )
        pool.require_current(request.worker)
        dispatcher.validate_request_state(request)

    def final_current():
        current()
        source_guard()
        pool.revalidate(request.worker)
        current()

    def verified_current():
        final_current()
        if verification_selection is not None:
            verification_selection.require_original_source(
                projection,
                policy,
                admission_guard=current,
                cancelled=cancelled,
            )
            final_current()

    final_current()
    outcome, content = dispatcher.dispatch_attested_record(request)
    if outcome.failure_code is not None:
        raise ActionWireError(
            outcome.failure_code,
            "retained PACKAGE worker refused its current action",
        )
    current()
    _require(
        outcome.request_identity == request.identity
        and outcome.failure_code is None
        and content is not None
        and record_identity(content) == outcome.result_identity
    )
    value = json.loads(content)
    _require(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "request_identity",
            "registration_identity",
            "execution",
            "admitted",
            "release_authorized",
        }
        and canonical_json_bytes(value) == content
        and value["schema"]
        == (
            RESULT_SCHEMA
            if verification_selection is None
            else VERIFICATION_RESULT_SCHEMA
        )
        and value["request_identity"] == request.identity.uri
        and value["admitted"] is False
        and value["release_authorized"] is False
    )
    registration = ContentIdentity.parse_uri(value["registration_identity"])
    profile = canonical_identity(
        {
            "schema": "literate-ai/retained-unit-package-worker-profile@1",
            "registration": registration.uri,
            "input": input_identity.uri,
        }
    )
    _require(pool.supports_package(request.worker, profile))
    execution = value["execution"]
    _require(
        type(execution) is dict
        and set(execution)
        == (
            _EXECUTION_FIELDS
            if verification_selection is None
            else _EXECUTION_FIELDS | {"operation", "verified_result_index"}
        )
        and execution["authorization_identity"] == authorization.uri
        and execution["retained_plan_identity"] == plan.identity.uri
        and type(execution["archive_bytes"]) is int
        and execution["archive_bytes"] > 0
        and type(execution["unit_count"]) is int
        and 1 <= execution["unit_count"] <= policy.max_units
    )
    if verification_selection is not None:
        _require(
            execution["operation"] == "verify"
            and execution["verified_result_index"]
            == verification_selection.result.to_dict()
            and execution["result_index"] == verification_selection.result.to_dict()
        )
    references = {
        name: BlobRef.from_dict(execution[name])
        for name in (
            "execution_request",
            "retained_receipt",
            "planning_evidence",
            "accepted_run_evidence",
            "link_proof",
            "result_index",
        )
    }
    _require(
        references["execution_request"].identity == authorization.uri
        and references["retained_receipt"].identity
        == execution["retained_receipt_identity"]
    )
    blob_source = CommandActionBlobSource(
        worker,
        dispatcher.deadline,
        registration_identity=registration,
        cwd=dispatcher.cwd,
        environment=dispatcher.environment,
    )
    custody = RetainedUnitReturnCustody(
        policy, store, blob_source, parent, dispatcher.deadline, current, cancelled
    )
    metadata = custody.metadata_store(
        maximum_bytes=min(64 * 1024**2, policy.max_provider_metadata_bytes)
    )
    _require(
        _document(metadata, references["execution_request"], 65536) == execution_request
    )
    inputs = read_retained_unit_planning_inputs(
        source,
        manifest,
        plan,
        component_locks,
        references["retained_receipt"],
        metadata,
        planning_evidence_reference=references["planning_evidence"],
        maximum_metadata_bytes=policy.max_provider_metadata_bytes,
        maximum_metadata_memory_bytes=policy.max_provider_memory_bytes,
    )
    run = reopen_retained_unit_run(
        inputs,
        planning,
        references["accepted_run_evidence"],
        policy=policy,
        test_receipt_policy=test_receipt_policy,
        admission_guard=current,
        expected_sdk_platform=hardware.os_family,
        expected_sdk_architecture={"amd64": "x86_64", "aarch64": "arm64"}.get(
            hardware.cpu_architecture.casefold(), hardware.cpu_architecture.casefold()
        ),
        current_sdk_snapshots=sdk_snapshots(),
    )
    projection = plan_retained_unit_packages(
        inputs, pkg_tool=pkg_tool, tar_tool=tar_tool, **policy.owner_arguments()
    )
    _require(
        projection.set_plan.identity.uri == execution["set_plan_identity"]
        and _document(metadata, references["link_proof"], policy.max_index_bytes)
        == retained_unit_link_document(
            run, inputs.receipt, projection, inputs.payload_manifest
        )
    )
    index, results, metrics = receive_retained_unit_package_set(
        projection,
        references["result_index"],
        store=store,
        source=blob_source,
        object_root=parent,
        deadline=dispatcher.deadline,
        admission_guard=current,
        cancelled=cancelled,
        custody=custody,
        max_descriptor_bytes=policy.max_descriptor_bytes,
        max_descriptor_total_bytes=policy.max_descriptor_total_bytes,
        max_index_bytes=policy.max_index_bytes,
        max_metadata_memory_bytes=policy.max_metadata_memory_bytes,
    )
    _require(
        index.archive_bytes == execution["archive_bytes"]
        and index.unit_count == execution["unit_count"]
    )
    final_current()
    # Retain the exact attested result assertion separately from the in-process
    # admission capability. It remains unadmitted when decoded on its own.
    record_ref = store.put_bytes(content, media_type="application/json")
    _require(record_ref.identity == outcome.result_identity.uri)
    capability = AdmittedRemoteRetainedUnitPackageSet(
        request,
        inputs,
        run,
        projection,
        index,
        results,
        tuple(
            references[name]
            for name in (
                "execution_request",
                "retained_receipt",
                "planning_evidence",
                "accepted_run_evidence",
                "link_proof",
                "result_index",
            )
        ),
        metrics,
        store,
        verified_current,
        verification_selection=verification_selection,
        _seal=_ADMITTED_REMOTE_SET,
    )
    capability.require_current()
    return capability
