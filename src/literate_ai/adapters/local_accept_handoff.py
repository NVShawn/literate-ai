"""Retain locally completed acceptance for independently verified remote LINK."""

from threading import Lock

from literate_ai.adapters.action_accept_record import AcceptWorkerInput
from literate_ai.adapters.action_accept_result_record import AcceptWorkerResult
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_provider_build import verify_accepted_component_records
from literate_ai.contracts import StandardComponentAcceptanceEvidence


class LocalAcceptanceLinkHandoff:
    def __init__(self, acceptor, indexer, ports, *, handoff_for):
        self.acceptor, self.indexer, self.ports = acceptor, indexer, ports
        self.handoff_for = handoff_for
        self._accepted, self._lock = {}, Lock()

    def retain_execution_provider_evidence(self, plan, scope, receipts):
        self.handoff_for.retain_execution_provider_evidence(plan, scope, receipts)
        receiver = getattr(self.acceptor, "retain_execution_provider_evidence", None)
        if callable(receiver):
            receiver(plan, scope, receipts)

    def accept(self, plan, test, execution):
        receipt = self.acceptor.accept(plan, test, execution)
        if not isinstance(receipt, StandardComponentAcceptanceEvidence) or (
            receipt.build.build_plan_identity != plan.identity
            or receipt.generated_tests.identity != test
            or receipt.execution.identity != execution
        ):
            raise ActionWireError(
                "action_link.acceptance_invalid", "exact local acceptance required"
            )
        with self._lock:
            self._accepted[receipt.identity] = (plan, receipt)
        return receipt

    def link_handoff(self, plan, receipt):
        def current():
            self.indexer.deadline.remaining()
            with self._lock:
                retained = self._accepted.get(receipt.identity)
            if retained != (plan, receipt) or self.ports.acceptance_stage_evidence(
                plan, receipt.generated_tests.identity, receipt.execution.identity
            ) != (receipt.build, receipt.generated_tests, receipt.execution):
                raise ActionWireError(
                    "action_link.acceptance_changed", "local acceptance custody differs"
                )

        current()
        value = self.handoff_for(
            plan, receipt.generated_tests.identity, receipt.execution.identity
        )
        raw = value.to_bytes()
        input_id = record_identity(raw)
        value = AcceptWorkerInput.admit(raw, input_id, self.indexer.deadline)
        build = value.execution_input.build_input
        if build.plan != plan or build.execution_plan != self.indexer.execution_plan:
            raise ActionWireError(
                "action_link.input_changed", "local LINK handoff differs"
            )
        records = self.ports.retained_evidence_records()
        reader = verify_accepted_component_records(
            records, receipt, build.source_validation, build.generation_plan
        )
        references = []
        for identity, content in records:
            if identity in reader.opened:
                current()
                reference = self.indexer.cas.put_bytes(content)
                if reference.identity != identity.uri:
                    raise ActionWireError(
                        "action_link.record_changed", "local LINK proof differs"
                    )
                references.append(reference)
        result = AcceptWorkerResult(
            input_id, receipt, tuple(sorted(references, key=lambda ref: ref.identity))
        )
        result_raw = result.to_bytes()
        result = AcceptWorkerResult.admit(
            result_raw,
            record_identity(result_raw),
            input_record=raw,
            input_identity=input_id,
            deadline=self.indexer.deadline,
        )
        current()
        return value, result
