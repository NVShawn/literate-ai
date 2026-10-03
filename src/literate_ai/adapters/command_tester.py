"""Queued TEST dispatch with exact BUILD handoff and controller proof admission."""

from functools import partial

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_test_record import (
    TestWorkerInput,
    required_test_toolchains,
)
from literate_ai.adapters.action_test_result import import_test_result
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class CommandComponentTester:
    """Share INDEX/BUILD capacity without local TEST fallback."""

    def __init__(
        self, indexer, admission, local_ports, *, handoff_for, result_source=None
    ):
        if (
            indexer.catalog.identity != admission.catalog.identity
            or indexer.workers != admission.workers
        ):
            raise ValueError("TEST and INDEX must share the same admitted worker pool")
        if not callable(handoff_for) or (
            result_source is not None and not callable(result_source)
        ):
            raise TypeError(
                "TEST handoff and result transport must be private callables"
            )
        self.indexer, self.admission, self.local_ports = indexer, admission, local_ports
        self.handoff_for, self.result_source = handoff_for, result_source
        self.eligible = tuple(
            worker.worker_id
            for worker in indexer.workers
            if admission.supports_phase(worker, LifecycleActionKind.TEST)
        )
        if not self.eligible:
            raise ValueError("no admitted TEST worker")
        self.nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.TEST
        }

    def _handoff(self, plan, exports):
        value = self.handoff_for(plan, exports)
        if not isinstance(value, TestWorkerInput):
            raise ActionWireError(
                "action_test.input_invalid", "typed TEST handoff required"
            )
        content = value.to_bytes()
        value = TestWorkerInput.admit(
            content, record_identity(content), self.indexer.deadline
        )
        build = value.build_input
        if (
            build.plan != plan
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
                "action_test.input_changed",
                "TEST handoff differs from current controller custody",
            )
        return value

    def _eligible_for(self, plan, exports):
        value = self._handoff(plan, exports)
        required = required_test_toolchains(value.build_input.inputs)
        eligible = tuple(
            worker.worker_id
            for worker in self.indexer.workers
            if worker.worker_id in self.eligible
            and self.admission.supports_test(worker, required)
        )
        if not eligible:
            raise ActionWireError(
                "action_test.tools_unavailable", "no compatible TEST worker"
            )
        return eligible

    def try_reserve_test(self, plan, exports):
        return self.indexer.slots.try_reserve(
            partial(self._execute, plan, exports),
            eligible_worker_ids=self._eligible_for(plan, exports),
        )

    def test(self, plan, exports):
        with self.indexer.slots.acquire(
            eligible_worker_ids=self._eligible_for(plan, exports)
        ) as (worker, slot):
            return self._execute(plan, exports, worker, slot)

    def _execute(self, plan, exports, worker, slot):
        value = self._handoff(plan, exports)
        content = value.to_bytes()
        input_identity = record_identity(content)

        def current():
            self.indexer.revalidate_worker(worker)
            if self._handoff(
                plan, exports
            ) != value or not self.admission.supports_test(
                worker, required_test_toolchains(value.build_input.inputs)
            ):
                raise ActionWireError(
                    "action_test.authority_changed", "TEST worker or handoff changed"
                )

        current()
        self.local_ports.retained_evidence_records()
        snapshot = self.indexer._snapshot(plan.request.source_tree_identity)
        if set(snapshot) != {item.path for item in value.build_input.files}:
            raise ActionWireError(
                "action_test.source_changed", "TEST source manifest changed"
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
                    "action_test.source_changed", "TEST source bytes changed"
                )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                value.build_input.execution_plan_identity,
                plan.component_revision,
                LifecycleActionKind.TEST,
                value.build_input.generation_plan_identity,
            )
        )
        records = {input_identity: content, record_identity(payload): payload}
        for identity, record in records.items():
            self.local_ports.retain_evidence_record(identity, record)
        current()
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-test-schedule@1",
                    "execution_plan": value.build_input.execution_plan_identity.uri,
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
            raise ActionWireError(outcome.failure_code, "worker TEST failed")
        result = results.get(outcome.result_identity)
        if result is None:
            raise ActionWireError(
                "action_test.result_missing", "worker TEST result missing"
            )
        selected = self.indexer.catalog.worker(worker.worker_id)
        return import_test_result(
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
