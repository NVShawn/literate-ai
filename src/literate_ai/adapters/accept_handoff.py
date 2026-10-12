"""Capture exact completed TEST and EXECUTE proof for a current ACCEPT handoff."""

from threading import Lock

from literate_ai.adapters.action_accept_record import AcceptWorkerInput
from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_build_record import validate_build_provider_receipts
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_execute_record import ExecuteWorkerInput
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.action_test_record import TestWorkerInput, TestWorkerResult
from literate_ai.adapters.qualification_capture import QualificationEvidenceReader
from literate_ai.adapters.standard_execution_admission import (
    verify_transferred_execution,
)
from literate_ai.adapters.standard_test_admission import verify_transferred_tests
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import (
    StandardComponentAcceptanceEvidence,
    StandardExecutionInputScope,
)


class _OpenedProof(QualificationEvidenceReader):
    def __init__(self, records):
        super().__init__(
            records,
            max_bytes=MAX_BUILD_EVIDENCE_BYTES,
            max_records=MAX_BUILD_EVIDENCE_RECORDS,
        )
        self.opened = set()

    def read_bytes(self, identity):
        content = super().read_bytes(identity)
        self.opened.add(identity)
        return content


class CompletedStagesAcceptHandoff:
    """Private EXECUTE-input composition supplies the complete runtime closure."""

    def __init__(self, indexer, ports, *, execution_input_for):
        if not callable(execution_input_for):
            raise TypeError("ACCEPT requires private completed-stage composition")
        self.indexer, self.ports = indexer, ports
        self.execution_input_for = execution_input_for
        self._receipts = {}
        self._receipt_lock = Lock()

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
                "action_accept.receipts_invalid",
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
                "action_accept.receipts_invalid", "runtime receipt artifacts differ"
            )
        key = plan.identity, scope.identity
        with self._receipt_lock:
            previous = self._receipts.get(key)
            if previous is not None and previous != receipts:
                raise ActionWireError(
                    "action_accept.receipts_changed",
                    "accepted runtime receipts changed",
                )
            self._receipts[key] = receipts

    def _execution_input(self, plan, exports, scope):
        with self._receipt_lock:
            receipts = self._receipts.get((plan.identity, scope.identity))
        if receipts is None:
            raise ActionWireError(
                "action_accept.receipts_missing", "runtime receipts were not admitted"
            )
        if (
            plan_standard_execution_receipts(
                self.indexer.execution_plan, plan, exports, receipts
            )
            != scope
        ):
            raise ActionWireError(
                "action_accept.scope_changed", "runtime receipt scope differs"
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
        value = self.execution_input_for(plan, exports, scope, providers, receipts)
        if not isinstance(value, ExecuteWorkerInput):
            raise TypeError("ACCEPT requires typed EXECUTE input")
        if (
            value.accepted_providers != receipts
            or value.provider_artifacts != providers
        ):
            raise ActionWireError(
                "action_accept.receipts_changed", "ACCEPT runtime custody differs"
            )
        return value

    def __call__(self, plan, test_identity, execution_identity):
        stages = self.ports.acceptance_stage_evidence(
            plan, test_identity, execution_identity
        )
        build, tests, executed = stages
        authority = executed.execution_authority
        if authority is None:
            raise ActionWireError(
                "action_accept.scope_missing",
                "ACCEPT requires scoped completed execution",
            )
        scope = authority.input_scope
        value = self._execution_input(plan, build.exports, scope)
        if not isinstance(value, ExecuteWorkerInput):
            raise TypeError("ACCEPT requires typed EXECUTE input")
        raw_execution = value.to_bytes()
        value = ExecuteWorkerInput.admit(
            raw_execution, record_identity(raw_execution), self.indexer.deadline
        )
        inputs = self.ports.build_execution_inputs(plan)
        source = self.ports.source_trees.evidence(plan.request.source_tree_identity)
        if (
            value.build_result.evidence != build
            or value.scope != scope
            or value.build_input.plan != plan
            or value.build_input.inputs != inputs
            or value.build_input.execution_plan != self.indexer.execution_plan
            or value.build_input.candidate != source.candidate
        ):
            raise ActionWireError(
                "action_accept.input_changed", "completed ACCEPT custody differs"
            )

        def current():
            self.indexer.deadline.remaining()
            if (
                self.ports.acceptance_stage_evidence(
                    plan, test_identity, execution_identity
                )
                != stages
                or self.ports.build_execution_inputs(plan) != inputs
                or self.ports.source_trees.evidence(plan.request.source_tree_identity)
                != source
                or self._execution_input(plan, build.exports, scope) != value
            ):
                raise ActionWireError(
                    "action_accept.input_changed", "completed ACCEPT authority changed"
                )

        current()
        records = self.ports.retained_evidence_records()
        test_reader = _OpenedProof(records)
        verify_transferred_tests(
            test_reader,
            plan=plan,
            build=build,
            source_custody=source,
            contract=inputs.contract,
            evidence=tests,
        )
        execution_reader = _OpenedProof(records)
        verify_transferred_execution(
            execution_reader,
            plan=plan,
            build=build,
            source_custody=source,
            contract=inputs.contract,
            evidence=executed,
            scope=scope,
            provider_artifacts=value.provider_artifacts,
            now=self.ports.clock(),
        )

        def capture(reader):
            refs = []
            for identity in sorted(reader.opened, key=lambda item: item.uri):
                current()
                ref = self.indexer.cas.put_bytes(reader.read_bytes(identity))
                if ref.identity != identity.uri:
                    raise ActionWireError(
                        "action_accept.proof_changed", "ACCEPT proof identity differs"
                    )
                refs.append(ref)
            return tuple(refs)

        raw_test = TestWorkerInput(value.build_input, value.build_result).to_bytes()
        result = AcceptWorkerInput(
            value,
            TestWorkerResult(record_identity(raw_test), tests, capture(test_reader)),
            ExecuteWorkerResult(
                record_identity(raw_execution), executed, capture(execution_reader)
            ),
        )
        content = result.to_bytes()
        result = AcceptWorkerInput.admit(
            content, record_identity(content), self.indexer.deadline
        )
        current()
        return result
