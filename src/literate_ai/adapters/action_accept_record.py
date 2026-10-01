"""Closed ACCEPT handoffs bind completed stages without selecting a weaker policy."""

import json
from dataclasses import dataclass

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_execute_record import ExecuteWorkerInput
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.action_test_record import TestWorkerInput, TestWorkerResult
from literate_ai.contracts import ContentIdentity, canonical_json_bytes

ACCEPT_INPUT_IDENTITY_ENV = "LITAI_ACCEPT_INPUT_IDENTITY"
ACCEPT_DEADLINE_ENV = "LITAI_ACCEPT_DEADLINE"
ACCEPT_CAS_ENV = "LITAI_ACCEPT_CAS"
ACCEPT_WORKSPACE_ENV = "LITAI_ACCEPT_WORKSPACE"


def _invalid():
    raise ActionWireError(
        "action_accept.input_invalid", "ACCEPT input authority refused"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


@dataclass(frozen=True)
class AcceptWorkerInput:
    execution_input: ExecuteWorkerInput
    test_result: TestWorkerResult
    execution_result: ExecuteWorkerResult

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema="literate-ai/accept-worker-input@1",
                execution_input=json.loads(self.execution_input.to_bytes()),
                test_result=json.loads(self.test_result.to_bytes()),
                execution_result=json.loads(self.execution_result.to_bytes()),
            )
        )

    @classmethod
    def admit(cls, content, identity, deadline):
        """Validate descriptors; the operation must still reopen all proof bytes."""
        deadline.remaining()
        if (
            not isinstance(content, bytes)
            or len(content) > MAX_ACTION_RECORD_BYTES
            or not isinstance(identity, ContentIdentity)
            or record_identity(content) != identity
        ):
            _invalid()
        try:
            value = json.loads(content, object_pairs_hook=_pairs)
            if (
                not isinstance(value, dict)
                or set(value)
                != {"schema", "execution_input", "test_result", "execution_result"}
                or value["schema"] != "literate-ai/accept-worker-input@1"
                or canonical_json_bytes(value) != content
            ):
                _invalid()
            raw_execution = canonical_json_bytes(value["execution_input"])
            execution_identity = record_identity(raw_execution)
            execution = ExecuteWorkerInput.admit(
                raw_execution, execution_identity, deadline
            )
            raw_test = TestWorkerInput(
                execution.build_input, execution.build_result
            ).to_bytes()
            raw_test_result = canonical_json_bytes(value["test_result"])
            tests = TestWorkerResult.admit(
                raw_test_result,
                record_identity(raw_test_result),
                input_record=raw_test,
                input_identity=record_identity(raw_test),
                deadline=deadline,
            )
            raw_execution_result = canonical_json_bytes(value["execution_result"])
            executed = ExecuteWorkerResult.admit(
                raw_execution_result,
                record_identity(raw_execution_result),
                input_record=raw_execution,
                input_identity=execution_identity,
                deadline=deadline,
            )
            # Shared proof is counted once, but contradictory descriptors for the
            # same content identity cannot use deduplication to evade the bound.
            refs = {}
            for result in (execution.build_result, tests, executed):
                for ref in result.evidence_records:
                    previous = refs.setdefault(ref.identity, ref)
                    if previous != ref:
                        _invalid()
            if (
                len(refs) > MAX_BUILD_EVIDENCE_RECORDS
                or sum(ref.size for ref in refs.values()) > MAX_BUILD_EVIDENCE_BYTES
            ):
                _invalid()
            deadline.remaining()
            return cls(execution, tests, executed)
        except ActionWireError:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            UnicodeError,
            RecursionError,
        ) as exc:
            raise ActionWireError(
                "action_accept.input_invalid", "ACCEPT envelope refused"
            ) from exc
