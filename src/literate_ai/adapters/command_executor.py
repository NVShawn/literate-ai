"""Queued EXECUTE dispatch with exact BUILD handoff and controller proof admission."""

from functools import partial
from threading import Lock

from literate_ai.adapters.action_build_record import validate_build_provider_receipts
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_execute import execute_predecessor_records
from literate_ai.adapters.action_execute_record import (
    ExecuteWorkerInput,
    required_execute_toolchains,
)
from literate_ai.adapters.action_execute_result import import_execute_result
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import (
    StandardComponentAcceptanceEvidence,
    StandardExecutionInputScope,
    canonical_identity,
    canonical_json_bytes,
)


class CommandComponentExecutor:
    """Share INDEX/BUILD capacity without local EXECUTE fallback."""

    def __init__(
        self, indexer, admission, local_ports, *, handoff_for, result_source=None
    ):
        if (
            indexer.catalog.identity != admission.catalog.identity
            or indexer.workers != admission.workers
        ):
            raise ValueError(
                "EXECUTE and INDEX must share the same admitted worker pool"
            )
        if not callable(handoff_for) or (
            result_source is not None and not callable(result_source)
        ):
            raise TypeError(
                "EXECUTE handoff and result transport must be private callables"
            )
        self.indexer, self.admission, self.local_ports = indexer, admission, local_ports
        self.handoff_for, self.result_source = handoff_for, result_source
        self._receipts = {}
        self._receipt_lock = Lock()
        self.eligible = tuple(
            worker.worker_id
            for worker in indexer.workers
            if admission.supports_phase(worker, LifecycleActionKind.EXECUTE)
        )
        if not self.eligible:
            raise ValueError("no admitted EXECUTE worker")
        self.nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.EXECUTE
        }

    def retain_execution_provider_evidence(self, plan, scope, receipts):
        if (
            not isinstance(scope, StandardExecutionInputScope)
            or scope.build_plan_identity != plan.identity
            or scope.component_revision != plan.component_revision
            or scope.execution_plan_identity != self.indexer.execution_plan.identity
            or scope.build_provider_artifact_identities
            != plan.provider_artifact_identities
            or not isinstance(receipts, tuple)
            or any(
                not isinstance(item, StandardComponentAcceptanceEvidence)
                for item in receipts
            )
        ):
            raise ActionWireError(
                "action_execute.receipts_invalid",
                "exact scoped runtime receipts required",
            )
        providers = tuple(
            sorted(
                (
                    artifact
                    for receipt in receipts
                    for artifact in receipt.build.exports
                ),
                key=lambda item: item.identity.uri,
            )
        )
        validate_build_provider_receipts(providers, receipts)
        if (
            tuple(item.identity for item in providers)
            != scope.provider_artifact_identities
        ):
            raise ActionWireError(
                "action_execute.receipts_invalid", "runtime receipt artifacts differ"
            )
        key = plan.identity, scope.identity
        with self._receipt_lock:
            previous = self._receipts.get(key)
            if previous is not None and previous != receipts:
                raise ActionWireError(
                    "action_execute.receipts_changed",
                    "accepted runtime receipts changed",
                )
            self._receipts[key] = receipts

    def _handoff(self, plan, exports, scope, providers):
        if not isinstance(scope, StandardExecutionInputScope):
            raise ActionWireError(
                "action_execute.scope_required",
                "remote EXECUTE requires current scoped inputs",
            )
        receipts = self._receipts.get((plan.identity, scope.identity))
        if receipts is None:
            raise ActionWireError(
                "action_execute.receipts_missing", "runtime receipts were not admitted"
            )
        try:
            expected = plan_standard_execution_receipts(
                self.indexer.execution_plan, plan, exports, receipts
            )
        except ValueError as exc:
            raise ActionWireError(
                "action_execute.scope_changed", "runtime scope differs"
            ) from exc
        expected_providers = tuple(
            sorted(
                (
                    artifact
                    for receipt in receipts
                    for artifact in receipt.build.exports
                ),
                key=lambda item: item.identity.uri,
            )
        )
        if expected != scope or providers != expected_providers:
            raise ActionWireError(
                "action_execute.scope_changed", "runtime receipt scope differs"
            )
        value = self.handoff_for(plan, exports, scope, providers, receipts)
        if not isinstance(value, ExecuteWorkerInput):
            raise ActionWireError(
                "action_execute.input_invalid", "typed EXECUTE handoff required"
            )
        content = value.to_bytes()
        value = ExecuteWorkerInput.admit(
            content, record_identity(content), self.indexer.deadline
        )
        build = value.build_input
        if (
            value.scope != scope
            or value.provider_artifacts != providers
            or value.accepted_providers != receipts
            or build.plan != plan
            or build.execution_plan != self.indexer.execution_plan
            or value.build_result.evidence.exports != exports
            or value.build_result.evidence
            != self.local_ports.build_evidence_for_test(plan, exports)
            or build.inputs != self.local_ports.build_execution_inputs(plan)
            or build.candidate
            != self.indexer._candidate(
                plan.component_revision, plan.request.source_tree_identity
            )
        ):
            raise ActionWireError(
                "action_execute.input_changed",
                "EXECUTE handoff differs from current controller custody",
            )
        return value

    def _eligible_for(self, plan, exports, scope, providers):
        value = self._handoff(plan, exports, scope, providers)
        required = required_execute_toolchains(value.build_input.inputs)
        eligible = tuple(
            worker.worker_id
            for worker in self.indexer.workers
            if worker.worker_id in self.eligible
            and self.admission.supports_execute(worker, required)
        )
        if not eligible:
            raise ActionWireError(
                "action_execute.tools_unavailable", "no compatible EXECUTE worker"
            )
        return eligible

    def try_reserve_execute(self, plan, exports, scope, providers):
        return self.indexer.slots.try_reserve(
            partial(self._execute, plan, exports, scope, providers),
            eligible_worker_ids=self._eligible_for(plan, exports, scope, providers),
        )

    def execute(self, plan, exports):
        raise ActionWireError(
            "action_execute.scope_required",
            "remote EXECUTE requires current scoped inputs",
        )

    def execute_scoped(self, plan, exports, scope, providers):
        with self.indexer.slots.acquire(
            eligible_worker_ids=self._eligible_for(plan, exports, scope, providers)
        ) as (worker, slot):
            return self._execute(plan, exports, scope, providers, worker, slot)

    def _execute(self, plan, exports, scope, providers, worker, slot):
        value = self._handoff(plan, exports, scope, providers)
        content = value.to_bytes()
        input_identity = record_identity(content)

        def current():
            self.indexer.revalidate_worker(worker)
            if self._handoff(
                plan, exports, scope, providers
            ) != value or not self.admission.supports_execute(
                worker, required_execute_toolchains(value.build_input.inputs)
            ):
                raise ActionWireError(
                    "action_execute.authority_changed",
                    "EXECUTE worker or handoff changed",
                )

        current()
        self.local_ports.retained_evidence_records()
        snapshot = self.indexer._snapshot(plan.request.source_tree_identity)
        if set(snapshot) != {item.path for item in value.build_input.files}:
            raise ActionWireError(
                "action_execute.source_changed", "EXECUTE source manifest changed"
            )
        for item in value.build_input.files:
            self.indexer.deadline.remaining()
            if (
                self.indexer.cas.put_bytes(
                    snapshot[item.path], media_type=item.blob.media_type
                )
                != item.blob
            ):
                raise ActionWireError(
                    "action_execute.source_changed", "EXECUTE source bytes changed"
                )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                value.build_input.execution_plan_identity,
                plan.component_revision,
                LifecycleActionKind.EXECUTE,
                value.build_input.generation_plan_identity,
            )
        )
        predecessor_identities, records = execute_predecessor_records(
            value, self.nodes[plan.component_revision]
        )
        records[record_identity(payload)] = payload
        for identity, record in records.items():
            self.local_ports.retain_evidence_record(identity, record)
        current()
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-execute-schedule@1",
                    "execution_plan": value.build_input.execution_plan_identity.uri,
                    "admission": self.admission.identity.uri,
                }
            ),
            self.nodes[plan.component_revision],
            worker,
            slot,
            predecessor_identities,
            self.indexer.deadline.identity,
        )
        results = {}
        outcome = self.indexer._dispatcher(records, results).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(outcome.failure_code, "worker EXECUTE failed")
        result = results.get(outcome.result_identity)
        if result is None:
            raise ActionWireError(
                "action_execute.result_missing", "worker EXECUTE result missing"
            )
        selected = self.indexer.catalog.worker(worker.worker_id)
        return import_execute_result(
            content=result,
            result_identity=outcome.result_identity,
            input_record=content,
            input_identity=input_identity,
            deadline=self.indexer.deadline,
            ports=self.local_ports,
            cas=self.indexer.cas,
            admission_guard=current,
            blob_source=None
            if self.result_source is None
            else partial(self.result_source, selected),
        )
