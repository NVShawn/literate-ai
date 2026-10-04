"""Closed BUILD child input custody shared by supervisor and worker child."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_provider_record import (
    ProviderBuildTransfer,
    validate_provider_transfers,
)
from literate_ai.adapters.action_source_index import (
    MAX_SOURCE_FILES,
    source_generation_result,
)
from literate_ai.adapters.source_evidence_validation import (
    SourceEvidenceValidationInputs,
)
from literate_ai.application.standard_build_inputs import (
    validate_standard_build_authority,
)
from literate_ai.application.standard_build_intent import StandardBuildIntentInputs
from literate_ai.application.standard_plan_finalization import (
    StandardPlanFinalizationInputs,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardComponentBuildIntent,
    StandardComponentBuildPlan,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    ContentIdentity,
    GeneratedSourceCandidate,
    StandardComponentAcceptanceEvidence,
    canonical_json_bytes,
)
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ComponentCommandContract,
    ComponentExecutionPlan,
    ComponentGenerationPlan,
)
from literate_ai.contracts.generation_cache import CachedSourceFile

BUILD_INPUT_IDENTITY_ENV = "LITAI_BUILD_INPUT_IDENTITY"
BUILD_DEADLINE_ENV = "LITAI_BUILD_DEADLINE"
BUILD_CAS_ENV = "LITAI_BUILD_CAS"
BUILD_WORKSPACE_ENV = "LITAI_BUILD_WORKSPACE"

_SCHEMA = "literate-ai/build-worker-input@1"
_FIELDS = frozenset(
    {
        "schema",
        "execution_plan_identity",
        "execution_plan",
        "generation_plan_identity",
        "generation_plan",
        "candidate",
        "plan",
        "intent",
        "authorization",
        "contract",
        "provider_artifacts",
        "accepted_providers",
        "provider_builds",
        "package_artifacts",
        "dependency_resolution",
        "files",
        "source_validation",
        "source_generation_identity",
        "source_custody_identity",
    }
)


def _invalid():
    raise ActionWireError(
        "action_build.input_invalid", "BUILD child input authority is invalid"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class BuildWorkerInput:
    execution_plan_identity: ContentIdentity
    generation_plan_identity: ContentIdentity
    candidate: GeneratedSourceCandidate
    plan: StandardComponentBuildPlan
    inputs: StandardPlanFinalizationInputs
    files: tuple[CachedSourceFile, ...]
    source_validation: SourceEvidenceValidationInputs
    source_generation_identity: ContentIdentity
    source_custody_identity: ContentIdentity
    generation_plan: ComponentGenerationPlan
    execution_plan: ComponentExecutionPlan
    accepted_providers: tuple[StandardComponentAcceptanceEvidence, ...] = ()
    provider_builds: tuple[ProviderBuildTransfer, ...] = ()

    def to_bytes(self) -> bytes:
        """Serialize authority; admission separately checks current time and custody."""
        return canonical_json_bytes(
            {
                "schema": _SCHEMA,
                "execution_plan_identity": self.execution_plan_identity.uri,
                "execution_plan": self.execution_plan.to_dict(),
                "generation_plan_identity": self.generation_plan_identity.uri,
                "generation_plan": self.generation_plan.to_dict(),
                "candidate": self.candidate.to_dict(),
                "plan": self.plan.to_dict(),
                "intent": self.inputs.intent.to_dict(),
                "authorization": self.inputs.authorization.to_dict(),
                "contract": self.inputs.contract.to_dict(),
                "provider_builds": [item.to_dict() for item in self.provider_builds],
                "accepted_providers": [
                    item.to_dict() for item in self.accepted_providers
                ],
                "provider_artifacts": [
                    item.to_dict() for item in self.inputs.providers
                ],
                "package_artifacts": [
                    item.to_dict() for item in self.inputs.package_artifacts
                ],
                "dependency_resolution": self.inputs.dependency_resolution,
                "files": [item.to_dict() for item in self.files],
                "source_validation": self.source_validation.to_dict(),
                "source_generation_identity": self.source_generation_identity.uri,
                "source_custody_identity": self.source_custody_identity.uri,
            }
        )

    @classmethod
    def admit(
        cls,
        content: bytes,
        identity: ContentIdentity,
        deadline: ActionDispatchDeadline,
        *,
        now: datetime,
    ) -> BuildWorkerInput:
        """Verify exact bytes and reconstruct authority without filesystem access."""
        deadline.remaining(now=now)
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
                or set(value) != _FIELDS
                or value["schema"] != _SCHEMA
            ):
                _invalid()
            for field, limit in (
                ("files", MAX_SOURCE_FILES),
                ("provider_artifacts", 4096),
                ("accepted_providers", 4096),
                ("provider_builds", 4096),
                ("package_artifacts", 4096),
            ):
                if not isinstance(value[field], list) or len(value[field]) > limit:
                    _invalid()
            mode = value["dependency_resolution"]
            if not isinstance(mode, str) or mode not in {"none", "npm", "python"}:
                _invalid()
            inputs = StandardPlanFinalizationInputs(
                StandardComponentBuildIntent.from_dict(value["intent"]),
                StandardBuildAuthorization.from_dict(value["authorization"]),
                ComponentCommandContract.from_dict(value["contract"]),
                tuple(
                    ArtifactExport.from_dict(item)
                    for item in value["provider_artifacts"]
                ),
                tuple(
                    ArtifactExport.from_dict(item)
                    for item in value["package_artifacts"]
                ),
                mode,
            )
            result = cls(
                ContentIdentity.parse_uri(value["execution_plan_identity"]),
                ContentIdentity.parse_uri(value["generation_plan_identity"]),
                GeneratedSourceCandidate.from_dict(value["candidate"]),
                StandardComponentBuildPlan.from_dict(value["plan"]),
                inputs,
                tuple(CachedSourceFile.from_dict(item) for item in value["files"]),
                SourceEvidenceValidationInputs.from_dict(value["source_validation"]),
                ContentIdentity.parse_uri(value["source_generation_identity"]),
                ContentIdentity.parse_uri(value["source_custody_identity"]),
                ComponentGenerationPlan.from_dict(value["generation_plan"]),
                ComponentExecutionPlan.from_dict(value["execution_plan"]),
                tuple(
                    StandardComponentAcceptanceEvidence.from_dict(item)
                    for item in value["accepted_providers"]
                ),
                tuple(
                    ProviderBuildTransfer.from_dict(item)
                    for item in value["provider_builds"]
                ),
            )
            if (
                result.execution_plan.identity != result.execution_plan_identity
                or result.generation_plan not in result.execution_plan.generation_plans
                or result.generation_plan.identity != result.generation_plan_identity
                or result.generation_plan.component_revision
                != result.candidate.component_revision
                or result.generation_plan.generation_key.identity
                != result.candidate.generation_key_identity
            ):
                _invalid()
            validate_build_provider_receipts(
                inputs.providers, result.accepted_providers
            )
            validate_provider_transfers(
                result.accepted_providers, result.provider_builds
            )
            revisions = {
                item.component_revision
                for item in result.execution_plan.generation_plans
            }
            if any(
                receipt.component_revision not in revisions
                for receipt in result.accepted_providers
            ):
                _invalid()
            expected_intent = StandardBuildIntentInputs(
                result.generation_plan_identity,
                result.candidate,
                inputs.contract,
                inputs.providers,
                inputs.package_artifacts,
                inputs.intent.native_sdk_input_identities,
                mode,
            ).create()
            if expected_intent != inputs.intent:
                _invalid()
            # Reuse INDEX's exact portable-path, size, ordering and tree checks.
            source_generation_result(
                result.execution_plan_identity, result.candidate, result.files
            )
            validate_standard_build_authority(result.plan, inputs, now=now)
            deadline.remaining()
            return result
        except ActionWireError:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
            RecursionError,
        ) as exc:
            raise ActionWireError(
                "action_build.input_invalid", "BUILD child input authority was refused"
            ) from exc


def required_build_toolchains(
    inputs: StandardPlanFinalizationInputs,
) -> tuple[ContentIdentity, ...]:
    """Exact tool requirements shared by controller placement and worker admission."""
    contract = inputs.contract
    required = {
        contract.tool_binding(ComponentCommandPhase.BUILD).toolchain_identity,
        contract.language_compiler_identity,
        contract.build_system_toolchain_identity,
    }
    if inputs.dependency_resolution == "npm":
        required.add(contract.language_runtime_identity)
    return tuple(sorted(required, key=lambda item: item.uri))


def validate_build_provider_receipts(providers, receipts) -> None:
    """Bind accepted receipt descriptors; artifact and proof bytes require transfer."""
    if (
        not isinstance(receipts, tuple)
        or len(receipts) > 4096
        or any(
            not isinstance(item, StandardComponentAcceptanceEvidence)
            for item in receipts
        )
    ):
        _invalid()
    revisions = tuple(item.component_revision.uri for item in receipts)
    if revisions != tuple(sorted(set(revisions))):
        _invalid()
    from literate_ai.application.standard_provider_receipts import (
        select_build_provider_receipts,
    )

    try:
        if select_build_provider_receipts(providers, receipts) != receipts:
            _invalid()
    except ValueError as exc:
        raise ActionWireError(
            "action_build.input_invalid", "BUILD provider receipt closure differs"
        ) from exc
