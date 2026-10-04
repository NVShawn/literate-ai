"""Closed EXECUTE handoffs bind completed BUILD and late runtime dependencies."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from literate_ai.adapters.action_build_record import (
    BuildWorkerInput,
    validate_build_provider_receipts,
)
from literate_ai.adapters.action_build_result import BuildWorkerResult
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_provider_record import (
    ProviderBuildTransfer,
    validate_provider_transfers,
)
from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_receipts,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    ContentIdentity,
    StandardComponentAcceptanceEvidence,
    StandardExecutionInputScope,
    canonical_json_bytes,
)

EXECUTE_INPUT_IDENTITY_ENV = "LITAI_EXECUTE_INPUT_IDENTITY"
EXECUTE_DEADLINE_ENV = "LITAI_EXECUTE_DEADLINE"
EXECUTE_CAS_ENV = "LITAI_EXECUTE_CAS"
EXECUTE_WORKSPACE_ENV = "LITAI_EXECUTE_WORKSPACE"


def _invalid():
    raise ActionWireError(
        "action_execute.input_invalid", "EXECUTE input authority refused"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


@dataclass(frozen=True)
class ExecuteWorkerInput:
    build_input: BuildWorkerInput
    build_result: BuildWorkerResult
    scope: StandardExecutionInputScope
    accepted_providers: tuple[StandardComponentAcceptanceEvidence, ...] = ()
    provider_builds: tuple[ProviderBuildTransfer, ...] = ()

    @property
    def provider_artifacts(self):
        return tuple(
            sorted(
                (
                    artifact
                    for receipt in self.accepted_providers
                    for artifact in receipt.build.exports
                ),
                key=lambda item: item.identity.uri,
            )
        )

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema="literate-ai/execute-worker-input@1",
                build_input=json.loads(self.build_input.to_bytes()),
                build_result=json.loads(self.build_result.to_bytes()),
                scope=self.scope.to_dict(),
                accepted_providers=[item.to_dict() for item in self.accepted_providers],
                provider_builds=[item.to_dict() for item in self.provider_builds],
            )
        )

    @classmethod
    def admit(cls, content, identity, deadline):
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
                != {
                    "schema",
                    "build_input",
                    "build_result",
                    "scope",
                    "accepted_providers",
                    "provider_builds",
                }
                or value["schema"] != "literate-ai/execute-worker-input@1"
                or canonical_json_bytes(value) != content
            ):
                _invalid()
            for field in ("accepted_providers", "provider_builds"):
                if not isinstance(value[field], list) or len(value[field]) > 4096:
                    _invalid()
            raw_input = canonical_json_bytes(value["build_input"])
            input_identity = record_identity(raw_input)
            build = BuildWorkerInput.admit(
                raw_input, input_identity, deadline, now=datetime.now(UTC)
            )
            raw_result = canonical_json_bytes(value["build_result"])
            result = cls(
                build,
                BuildWorkerResult.admit(
                    raw_result,
                    record_identity(raw_result),
                    input_record=raw_input,
                    input_identity=input_identity,
                    deadline=deadline,
                ),
                StandardExecutionInputScope.from_dict(value["scope"]),
                tuple(
                    StandardComponentAcceptanceEvidence.from_dict(item)
                    for item in value["accepted_providers"]
                ),
                tuple(
                    ProviderBuildTransfer.from_dict(item)
                    for item in value["provider_builds"]
                ),
            )
            providers = result.provider_artifacts
            validate_build_provider_receipts(providers, result.accepted_providers)
            validate_provider_transfers(
                result.accepted_providers, result.provider_builds
            )
            # Combined bounds apply to BUILD and runtime custody, deduplicated by
            # content identity. Runtime inputs never rewrite the BUILD descriptor.
            validate_provider_transfers(
                build.accepted_providers + result.accepted_providers,
                build.provider_builds + result.provider_builds,
            )
            receipts = {
                item.component_revision: item for item in result.accepted_providers
            }
            if any(
                receipts.get(item.component_revision) != item
                for item in build.accepted_providers
            ) or result.scope != plan_standard_execution_receipts(
                build.execution_plan,
                build.plan,
                result.build_result.evidence.exports,
                result.accepted_providers,
            ):
                _invalid()
            deadline.remaining()
            build.inputs.authorization.grant.require_valid(
                build.inputs.intent.build_request, now=datetime.now(UTC)
            )
            return result
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
                "action_execute.input_invalid", "EXECUTE envelope refused"
            ) from exc


def required_execute_toolchains(inputs):
    """Select every EXECUTE runtime without requiring BUILD-only tools."""
    contract = inputs.contract
    contracts = (
        contract.entrypoint_command_contracts()
        if contract.is_multi_entrypoint
        else (contract,)
    )
    return tuple(
        sorted(
            {
                item.tool_binding(ComponentCommandPhase.EXECUTE).toolchain_identity
                for item in contracts
            },
            key=lambda item: item.uri,
        )
    )
