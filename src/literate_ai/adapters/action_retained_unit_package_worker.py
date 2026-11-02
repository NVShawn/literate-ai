"""Private composition of one exact retained-unit PACKAGE action.

The wire carries bounded intent only. The registered callback runs the canonical
retained lifecycle, including original source/SDK owners, independent acceptance
and live LINK. No parser can restore those owners from a returned document.
"""

from dataclasses import dataclass

from literate_ai.adapters.action_capabilities import package_profile_identity
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.retained_receiver_context import (
    _admit_retained_receiver_context,
)
from literate_ai.adapters.retained_unit_inspection import (
    retained_unit_metadata_reference,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionNode,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)

INPUT_SCHEMA = "literate-ai/retained-unit-package-worker-input@1"
RESULT_SCHEMA = "literate-ai/retained-unit-package-worker-result@1"
MAX_RETAINED_PACKAGE_INTENT_BYTES = 65536


def retained_unit_package_input(execution_request):
    """Preserve the public execution request inside a distinct PACKAGE family."""
    content = canonical_json_bytes(
        {"schema": INPUT_SCHEMA, "execution_request": execution_request}
    )
    if len(content) > MAX_RETAINED_PACKAGE_INTENT_BYTES:
        raise ValueError("retained.unit.bounded-worker-intent-required")
    return content


def retained_unit_package_action(plan_identity, worker_id, input_identity):
    return LifecycleActionNode(
        "retained-unit-package:" + plan_identity.digest,
        plan_identity,
        LifecycleActionKind.PACKAGE,
        input_identity,
        (),
        (worker_id,),
    )


@dataclass(frozen=True)
class ConfiguredRetainedUnitPackageWorker:
    worker: ExecutionWorker
    catalog_identity: ContentIdentity
    retained_plan_identity: ContentIdentity
    registration_identity: ContentIdentity
    execution_authorization: ContentIdentity
    execution_request_bytes: bytes
    run_retained: object
    require_current: object
    poll_guard: object = None

    def __post_init__(self):
        if (
            not isinstance(self.worker, ExecutionWorker)
            or self.worker.kind is not ExecutionWorkerKind.SSH
            or any(
                not isinstance(value, ContentIdentity)
                for value in (
                    self.catalog_identity,
                    self.retained_plan_identity,
                    self.registration_identity,
                    self.execution_authorization,
                )
            )
            or not isinstance(self.execution_request_bytes, bytes)
            or not 0
            < len(self.execution_request_bytes)
            <= MAX_RETAINED_PACKAGE_INTENT_BYTES
            or not callable(self.run_retained)
            or not callable(self.require_current)
            or self.poll_guard is not None
            and not callable(self.poll_guard)
        ):
            raise TypeError("retained.unit.private-package-registration-required")

    @property
    def identity(self):
        return package_profile_identity(
            canonical_identity(
                {
                    "schema": "literate-ai/retained-unit-package-worker-profile@1",
                    "registration": self.registration_identity.uri,
                    "input": record_identity(self.execution_request_bytes).uri,
                }
            )
        )

    def admit_request(
        self, request, deadline, records, expected_worker_identity, capability_identity
    ):
        # Exact equality to private current intent precedes executing anything.
        # This new family never enters legacy generated-Component PACKAGE decode.
        identity = record_identity(self.execution_request_bytes)
        if (
            expected_worker_identity != self.worker.identity
            or request.worker.worker_identity != self.worker.identity
            or request.worker.worker_id != self.worker.worker_id
            or request.worker.catalog_identity != self.catalog_identity
            or request.action
            != retained_unit_package_action(
                self.retained_plan_identity, self.worker.worker_id, identity
            )
            or request.predecessor_result_identities
            or request.input_record_identities != (identity,)
            or records != {identity: self.execution_request_bytes}
            or request.deadline_identity != deadline.identity
            or not isinstance(capability_identity, ContentIdentity)
        ):
            raise ActionWireError(
                "retained_unit_package.request_mismatch",
                "retained PACKAGE differs from its private authorization",
            )

        deadline.remaining()

    def execute(
        self,
        request,
        deadline,
        records,
        *,
        expected_worker_identity,
        cas,
        admission_guard,
        capability_identity,
        measure_capability,
        blob_source=None,
        cancelled=lambda: False,
    ):
        self.admit_request(
            request, deadline, records, expected_worker_identity, capability_identity
        )

        def current():
            deadline.remaining()
            if cancelled():
                raise ActionWireError(
                    "retained_unit_package.cancelled", "retained PACKAGE was cancelled"
                )
            self.require_current()
            admission_guard()
            deadline.remaining()

        current()
        from literate_ai.adapters.retained_package_capability import (
            RegisteredRetainedPackageCapabilityProbe,
        )

        registration_guard = current
        if type(measure_capability) is RegisteredRetainedPackageCapabilityProbe:
            measure_capability.require_binding(
                self,
                request,
                deadline,
                expected_worker_identity,
                capability_identity,
                admission_guard,
            )

            def registration_guard():
                deadline.remaining()
                if cancelled():
                    raise ActionWireError(
                        "retained_unit_package.cancelled",
                        "retained PACKAGE was cancelled",
                    )
                measure_capability.require_registered_current()
                deadline.remaining()

        context = _admit_retained_receiver_context(
            request,
            deadline,
            worker=self.worker,
            catalog_identity=self.catalog_identity,
            retained_plan_identity=self.retained_plan_identity,
            capability_identity=capability_identity,
            registration_guard=registration_guard,
            measure_capability=measure_capability,
            cancelled=cancelled,
        )
        try:
            result = self.run_retained(context)
            context.require_current()
            # Only exact CAS roots are returned. Host paths, SDK handles and live
            # owners are deliberately absent from this data-only assertion.
            if (
                type(result) is not dict
                or result.get("retained_plan_identity")
                != self.retained_plan_identity.uri
                or result.get("authorization_identity")
                != self.execution_authorization.uri
                or result.get("admitted") is not True
                or result.get("native_verified") is not True
                or result.get("release_authorized") is not False
            ):
                raise ValueError("retained.unit.complete-native-execution-required")
            selected = {
                name: result[name]
                for name in (
                    "authorization_identity",
                    "execution_request",
                    "retained_plan_identity",
                    "retained_receipt_identity",
                    "planning_evidence",
                    "accepted_run_evidence",
                    "link_proof",
                    "set_plan_identity",
                    "result_index",
                    "archive_bytes",
                    "unit_count",
                )
            }
            selected["retained_receipt"] = retained_unit_metadata_reference(
                cas,
                ContentIdentity.parse_uri(selected["retained_receipt_identity"]),
                maximum_bytes=64 * 1024**2,
            ).to_dict()
            for name in (
                "retained_receipt",
                "execution_request",
                "planning_evidence",
                "accepted_run_evidence",
                "link_proof",
                "result_index",
            ):
                cas.verify(BlobRef.from_dict(selected[name]))
            if (
                BlobRef.from_dict(selected["execution_request"]).identity
                != self.execution_authorization.uri
            ):
                raise ValueError("retained.unit.execution-request-retention-differs")
            current()
            return canonical_json_bytes(
                {
                    "schema": RESULT_SCHEMA,
                    "request_identity": request.identity.uri,
                    "registration_identity": self.registration_identity.uri,
                    "execution": selected,
                    "admitted": False,
                    "release_authorized": False,
                }
            )
        finally:
            context.close()
