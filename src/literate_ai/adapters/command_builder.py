"""Queued command BUILD dispatch and verified controller artifact admission."""

from datetime import UTC, datetime
from functools import partial

from literate_ai.adapters.action_build_record import (
    BuildWorkerInput,
    required_build_toolchains,
    validate_build_provider_receipts,
)
from literate_ai.adapters.action_build_result import (
    BuildWorkerResult,
    import_build_result,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_test_record import TestWorkerInput
from literate_ai.adapters.build_handoff import capture_build_input
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class CommandComponentBuilder:
    """Dispatch only to admitted BUILD workers; result transport is private binding."""

    def __init__(self, indexer, admission, local_ports, *, result_source=None):
        if (
            indexer.catalog.identity != admission.catalog.identity
            or indexer.workers != admission.workers
        ):
            raise ValueError("BUILD and INDEX must share the same admitted worker pool")
        if result_source is not None and not callable(result_source):
            raise TypeError("BUILD result source must be privately callable")
        self.indexer, self.admission, self.local_ports = indexer, admission, local_ports
        self.result_source = result_source
        self._provider_receipts = {}
        self._test_handoffs = {}
        self.eligible = tuple(
            worker.worker_id
            for worker in indexer.workers
            if admission.supports_phase(worker, LifecycleActionKind.BUILD)
        )
        if not self.eligible:
            raise ValueError("no admitted BUILD worker")
        self.nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.BUILD
        }
        self.generations = {
            plan.component_revision: plan
            for plan in indexer.execution_plan.generation_plans
        }

    def test_handoff(self, plan, exports):
        """Read only a successfully imported BUILD handoff under current authority."""
        value = self._test_handoffs.get(plan.identity)
        if value is None:
            raise ActionWireError(
                "action_test.build_missing", "successful BUILD handoff unavailable"
            )
        content = value.to_bytes()
        admitted = TestWorkerInput.admit(
            content, record_identity(content), self.indexer.deadline
        )
        if (
            admitted.build_input.plan != plan
            or admitted.build_result.evidence.exports != exports
            or self.local_ports.build_execution_inputs(plan)
            != admitted.build_input.inputs
        ):
            raise ActionWireError(
                "action_test.input_changed",
                "BUILD handoff differs from current TEST inputs",
            )
        return admitted

    def retain_build_provider_evidence(self, plan, receipts):
        inputs = self.local_ports.build_execution_inputs(plan)
        if plan.component_revision not in self.generations:
            raise ActionWireError(
                "action_build.plan_invalid", "BUILD plan is not scheduled"
            )
        validate_build_provider_receipts(inputs.providers, receipts)
        self._provider_receipts[plan.component_revision] = (plan.identity, receipts)

    def _receipts_for(self, plan, inputs):
        retained = self._provider_receipts.get(plan.component_revision)
        receipts = () if retained is None else retained[1]
        if retained is not None and retained[0] != plan.identity:
            raise ActionWireError(
                "action_build.providers_changed", "provider receipt plan differs"
            )
        validate_build_provider_receipts(inputs.providers, receipts)
        return receipts

    def _eligible_for(self, plan):
        inputs = self.local_ports.build_execution_inputs(plan)
        self._receipts_for(plan, inputs)
        required = required_build_toolchains(inputs)
        eligible = tuple(
            worker.worker_id
            for worker in self.indexer.workers
            if worker.worker_id in self.eligible
            and self.admission.supports_build(worker, required)
        )
        if not eligible:
            raise ActionWireError(
                "action_build.tools_unavailable", "no compatible BUILD worker"
            )
        return eligible

    def try_reserve_build(self, plan, provider_artifacts):
        return self.indexer.slots.try_reserve(
            partial(self._execute, plan, provider_artifacts),
            eligible_worker_ids=self._eligible_for(plan),
        )

    def build(self, plan, provider_artifacts):
        with self.indexer.slots.acquire(
            eligible_worker_ids=self._eligible_for(plan)
        ) as (
            worker,
            slot,
        ):
            return self._execute(plan, provider_artifacts, worker, slot)

    def _execute(self, plan, provider_artifacts, worker, slot):
        def require_worker():
            self.indexer.require_worker_current(worker)
            if not self.admission.supports_build(
                worker,
                required_build_toolchains(
                    self.local_ports.build_execution_inputs(plan)
                ),
            ):
                raise ActionWireError(
                    "action_build.unsupported_phase", "worker is not admitted for BUILD"
                )

        require_worker()
        inputs = self.local_ports.build_execution_inputs(plan)
        value = capture_build_input(
            self.indexer,
            self.local_ports,
            plan,
            provider_artifacts,
            self._receipts_for(plan, inputs),
        )
        content = value.to_bytes()
        input_identity = record_identity(content)
        BuildWorkerInput.admit(
            content, input_identity, self.indexer.deadline, now=datetime.now(UTC)
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                value.execution_plan_identity,
                plan.component_revision,
                LifecycleActionKind.BUILD,
                value.generation_plan_identity,
            )
        )
        records = {input_identity: content, record_identity(payload): payload}
        for identity, record in records.items():
            self.local_ports.retain_evidence_record(identity, record)
        if self.local_ports.build_execution_inputs(plan) != inputs:
            raise ActionWireError("action_build.inputs_changed", "BUILD inputs changed")
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-build-schedule@1",
                    "execution_plan": value.execution_plan_identity.uri,
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
            raise ActionWireError(outcome.failure_code, "worker BUILD failed")
        result = results.get(outcome.result_identity)
        if result is None:
            raise ActionWireError(
                "action_build.result_missing", "worker BUILD result missing"
            )
        selected = self.indexer.catalog.worker(worker.worker_id)
        handoff = TestWorkerInput(
            value,
            BuildWorkerResult.admit(
                result,
                outcome.result_identity,
                input_record=content,
                input_identity=input_identity,
                deadline=self.indexer.deadline,
            ),
        )
        output = import_build_result(
            content=result,
            result_identity=outcome.result_identity,
            input_record=content,
            input_identity=input_identity,
            deadline=self.indexer.deadline,
            ports=self.local_ports,
            cas=self.indexer.cas,
            retain_record=self.local_ports.retain_evidence_record,
            blob_source=None
            if self.result_source is None
            else partial(self.result_source, selected),
            admission_guard=require_worker,
        )
        self._test_handoffs[plan.identity] = handoff
        return output
