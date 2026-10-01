"""Compose completed BUILD custody with the current full runtime provider closure."""

from literate_ai.adapters.action_build_record import validate_build_provider_receipts
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_execute_record import ExecuteWorkerInput
from literate_ai.adapters.build_handoff import capture_provider_transfers
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)


class CompletedBuildExecuteHandoff:
    """Reopen provider bytes on every admission; never reuse unchecked archives."""

    def __init__(self, builder, indexer, ports):
        self.builder, self.indexer, self.ports = builder, indexer, ports

    def __call__(self, plan, exports, scope, providers, receipts):
        validate_build_provider_receipts(providers, receipts)
        expected = plan_standard_execution_receipts(
            self.indexer.execution_plan, plan, exports, receipts
        )
        if expected != scope:
            raise ActionWireError(
                "action_execute.scope_changed", "runtime receipt scope differs"
            )
        completed = self.builder.test_handoff(plan, exports)

        def current():
            self.indexer.deadline.remaining()
            if (
                self.builder.test_handoff(plan, exports) != completed
                or completed.build_input.execution_plan != self.indexer.execution_plan
                or completed.build_input.inputs
                != self.ports.build_execution_inputs(plan)
                or completed.build_result.evidence
                != self.ports.build_evidence_for_test(plan, exports)
                or completed.build_input.candidate
                != self.indexer._candidate(
                    plan.component_revision, plan.request.source_tree_identity
                )
            ):
                raise ActionWireError(
                    "action_execute.input_changed", "completed BUILD custody changed"
                )

        current()
        transfers = capture_provider_transfers(
            self.indexer, self.ports, receipts, current
        )
        value = ExecuteWorkerInput(
            completed.build_input, completed.build_result, scope, receipts, transfers
        )
        content = value.to_bytes()
        return ExecuteWorkerInput.admit(
            content, record_identity(content), self.indexer.deadline
        )
