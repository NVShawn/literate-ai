"""Queued ACCEPT dispatch with exact BUILD handoff and controller proof admission."""

from functools import partial
from threading import Lock

from literate_ai.adapters.action_accept_record import (
    AcceptWorkerInput,
)
from literate_ai.adapters.action_accept_result import import_accept_result
from literate_ai.adapters.action_accept_result_record import AcceptWorkerResult
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts import (
    StandardComponentAcceptanceEvidence,
    canonical_identity,
    canonical_json_bytes,
)


class CommandComponentAcceptor:
    """Share INDEX/BUILD capacity without local ACCEPT fallback."""

    def __init__(
        self, indexer, admission, local_ports, *, handoff_for, result_source=None
    ):
        if (
            indexer.catalog.identity != admission.catalog.identity
            or indexer.workers != admission.workers
        ):
            raise ValueError(
                "ACCEPT and INDEX must share the same admitted worker pool"
            )
        if not callable(handoff_for) or (
            result_source is not None and not callable(result_source)
        ):
            raise TypeError(
                "ACCEPT handoff and result transport must be private callables"
            )
        self.indexer, self.admission, self.local_ports = indexer, admission, local_ports
        self.handoff_for, self.result_source = handoff_for, result_source
        self._link_handoffs = {}
        self._link_lock = Lock()
        self.eligible = tuple(
            worker.worker_id
            for worker in indexer.workers
            if admission.supports_phase(worker, LifecycleActionKind.ACCEPT)
        )
        if not self.eligible:
            raise ValueError("no admitted ACCEPT worker")
        self.nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.ACCEPT
        }

    def retain_execution_provider_evidence(self, plan, scope, receipts):
        receive = getattr(self.handoff_for, "retain_execution_provider_evidence", None)
        if not callable(receive):
            raise ActionWireError(
                "action_accept.receipts_unavailable",
                "ACCEPT receipt intake unavailable",
            )
        receive(plan, scope, receipts)

    def _handoff(self, plan, test, execution):
        value = self.handoff_for(plan, test, execution)
        if not isinstance(value, AcceptWorkerInput):
            raise ActionWireError(
                "action_accept.input_invalid", "typed ACCEPT handoff required"
            )
        content = value.to_bytes()
        value = AcceptWorkerInput.admit(
            content, record_identity(content), self.indexer.deadline
        )
        build = value.execution_input.build_input
        stages = self.local_ports.acceptance_stage_evidence(plan, test, execution)
        if (
            build.plan != plan
            or build.execution_plan != self.indexer.execution_plan
            or (
                value.execution_input.build_result.evidence,
                value.test_result.evidence,
                value.execution_result.evidence,
            )
            != stages
            or build.inputs != self.local_ports.build_execution_inputs(plan)
            or build.candidate
            != self.indexer._candidate(
                plan.component_revision, plan.request.source_tree_identity
            )
        ):
            raise ActionWireError(
                "action_accept.input_changed",
                "ACCEPT handoff differs from current controller custody",
            )
        return value

    def link_handoff(self, plan, receipt):
        """Expose only imported ACCEPT custody, rechecked against current stages."""
        if not isinstance(receipt, StandardComponentAcceptanceEvidence):
            raise TypeError("LINK handoff requires typed acceptance evidence")
        with self._link_lock:
            retained = self._link_handoffs.get(receipt.identity)
        if retained is None:
            raise ActionWireError(
                "action_accept.link_unavailable", "verified ACCEPT handoff unavailable"
            )
        value, result = retained
        current = self._handoff(
            plan,
            receipt.generated_tests.identity,
            receipt.execution.identity,
        )
        if current != value or result.evidence != receipt:
            raise ActionWireError(
                "action_accept.input_changed", "LINK acceptance custody changed"
            )
        raw_input, raw_result = value.to_bytes(), result.to_bytes()
        admitted = AcceptWorkerResult.admit(
            raw_result,
            record_identity(raw_result),
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=self.indexer.deadline,
        )
        return current, admitted

    def _eligible_for(self, plan, test, execution):
        self._handoff(plan, test, execution)
        eligible = tuple(
            worker.worker_id
            for worker in self.indexer.workers
            if worker.worker_id in self.eligible
            and self.admission.supports_phase(worker, LifecycleActionKind.ACCEPT)
        )
        if not eligible:
            raise ActionWireError(
                "action_accept.unavailable", "no compatible ACCEPT worker"
            )
        return eligible

    def try_reserve_accept(self, plan, test, execution):
        return self.indexer.slots.try_reserve(
            partial(self._execute, plan, test, execution),
            eligible_worker_ids=self._eligible_for(plan, test, execution),
        )

    def accept(self, plan, test, execution):
        with self.indexer.slots.acquire(
            eligible_worker_ids=self._eligible_for(plan, test, execution)
        ) as (worker, slot):
            return self._execute(plan, test, execution, worker, slot)

    def _execute(self, plan, test, execution, worker, slot):
        value = self._handoff(plan, test, execution)
        content = value.to_bytes()
        input_identity = record_identity(content)

        def current():
            self.indexer.require_worker_current(worker)
            if self._handoff(
                plan, test, execution
            ) != value or not self.admission.supports_phase(
                worker, LifecycleActionKind.ACCEPT
            ):
                raise ActionWireError(
                    "action_accept.authority_changed",
                    "ACCEPT worker or handoff changed",
                )

        current()
        self.local_ports.retained_evidence_records()
        snapshot = self.indexer._snapshot(plan.request.source_tree_identity)
        if set(snapshot) != {
            item.path for item in value.execution_input.build_input.files
        }:
            raise ActionWireError(
                "action_accept.source_changed", "ACCEPT source manifest changed"
            )
        for item in value.execution_input.build_input.files:
            self.indexer.deadline.remaining()
            if (
                self.indexer.cas.put_bytes(
                    snapshot[item.path], media_type=item.blob.media_type
                )
                != item.blob
            ):
                raise ActionWireError(
                    "action_accept.source_changed", "ACCEPT source bytes changed"
                )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                value.execution_input.build_input.execution_plan_identity,
                plan.component_revision,
                LifecycleActionKind.ACCEPT,
                value.execution_input.build_input.generation_plan_identity,
            )
        )
        records = {input_identity: content, record_identity(payload): payload}
        for identity, record in records.items():
            self.local_ports.retain_evidence_record(identity, record)
        current()
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-accept-schedule@1",
                    "execution_plan": (
                        value.execution_input.build_input.execution_plan_identity.uri
                    ),
                    "admission": self.admission.identity.uri,
                }
            ),
            self.nodes[plan.component_revision],
            worker,
            slot,
            (input_identity,),
            self.indexer.deadline.identity,
        )
        results = {}
        outcome = self.indexer._dispatcher(records, results).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(outcome.failure_code, "worker ACCEPT failed")
        result = results.get(outcome.result_identity)
        if result is None:
            raise ActionWireError(
                "action_accept.result_missing", "worker ACCEPT result missing"
            )
        selected = self.indexer.catalog.worker(worker.worker_id)
        receipt = import_accept_result(
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
        current()
        admitted_result = AcceptWorkerResult.admit(
            result,
            outcome.result_identity,
            input_record=content,
            input_identity=input_identity,
            deadline=self.indexer.deadline,
        )
        with self._link_lock:
            existing = self._link_handoffs.get(receipt.identity)
            handoff = (value, admitted_result)
            if existing is not None and existing != handoff:
                raise ActionWireError(
                    "action_accept.input_changed", "accepted LINK handoff changed"
                )
            self._link_handoffs[receipt.identity] = handoff
        return receipt
