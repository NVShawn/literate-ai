"""Capture a locally completed BUILD for TEST on an admitted command worker."""

from literate_ai.adapters.action_build_record import validate_build_provider_receipts
from literate_ai.adapters.action_build_result import (
    BuildWorkerResult,
    capture_build_result,
)
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_test_record import TestWorkerInput
from literate_ai.adapters.build_handoff import capture_build_input
from literate_ai.application.standard_lifecycle_ports import (
    BuildProviderEvidenceReceiver,
)


class LocalBuildTestHandoff:
    """Wrap local BUILD custody without executing TEST or granting remote BUILD."""

    def __init__(self, builder, indexer, ports):
        self.builder, self.indexer, self.ports = builder, indexer, ports
        self._receipts, self._handoffs = {}, {}

    def retain_build_provider_evidence(self, plan, receipts):
        inputs = self.ports.build_execution_inputs(plan)
        validate_build_provider_receipts(inputs.providers, receipts)
        if isinstance(self.builder, BuildProviderEvidenceReceiver):
            self.builder.retain_build_provider_evidence(plan, receipts)
        self._receipts[plan.identity] = receipts

    def build(self, plan, providers):
        receipts = self._receipts.get(plan.identity, ())
        value = capture_build_input(self.indexer, self.ports, plan, providers, receipts)
        output = self.builder.build(plan, providers)
        content = value.to_bytes()
        identity = record_identity(content)
        result = capture_build_result(
            input_record=content,
            input_identity=identity,
            deadline=self.indexer.deadline,
            ports=self.ports,
            output=output,
            records=self.ports.retained_evidence_records(),
            cas=self.indexer.cas,
        )
        handoff = TestWorkerInput(
            value,
            BuildWorkerResult.admit(
                result,
                record_identity(result),
                input_record=content,
                input_identity=identity,
                deadline=self.indexer.deadline,
            ),
        )
        if self.ports.build_execution_inputs(plan) != value.inputs:
            raise ActionWireError(
                "action_test.input_changed", "local BUILD inputs changed"
            )
        self._handoffs[plan.identity] = handoff
        return output

    def test_handoff(self, plan, exports):
        value = self._handoffs.get(plan.identity)
        if value is None:
            raise ActionWireError(
                "action_test.build_missing", "completed local BUILD handoff unavailable"
            )
        content = value.to_bytes()
        admitted = TestWorkerInput.admit(
            content, record_identity(content), self.indexer.deadline
        )
        if (
            admitted.build_input.plan != plan
            or admitted.build_result.evidence.exports != exports
            or self.ports.build_execution_inputs(plan) != admitted.build_input.inputs
            or self.ports.build_evidence_for_test(plan, exports)
            != admitted.build_result.evidence
        ):
            raise ActionWireError(
                "action_test.input_changed", "local BUILD handoff differs"
            )
        return admitted
