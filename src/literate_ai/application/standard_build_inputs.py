"""Portable locked-command BUILD preflight without host or cache access."""

from datetime import datetime

from literate_ai.application.standard_plan_finalization import (
    StandardPlanFinalizationInputs,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildPlan,
)
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ComponentCommandContract,
)


class StandardBuildInputError(ValueError):
    """A build plan or provider set differs from the locked command contract."""


def validate_standard_build_inputs(
    plan: StandardComponentBuildPlan,
    provider_artifacts: tuple[ArtifactExport, ...],
    contract: ComponentCommandContract,
) -> None:
    declared_shapes = {
        item.export_id: item for item in plan.manifest.export_declarations
    }
    expected_shapes = {
        item.export_id: item for item in contract.artifact_export_shapes()
    }
    if set(declared_shapes) != set(expected_shapes) or any(
        (
            declaration.component_revision,
            declaration.role,
            declaration.abi_identity,
            declaration.target_identity,
            declaration.media_type,
            declaration.producer_identity,
            declaration.toolchain_identity,
        )
        != (
            contract.component_revision,
            expected_shapes[export_id].role,
            expected_shapes[export_id].abi_identity,
            expected_shapes[export_id].target_identity,
            expected_shapes[export_id].media_type,
            expected_shapes[export_id].producer_identity,
            contract.language_compiler_identity,
        )
        for export_id, declaration in declared_shapes.items()
    ):
        raise StandardBuildInputError(
            "build plan does not realize the exact locked command export shape"
        )
    if tuple(item.identity for item in provider_artifacts) != (
        plan.provider_artifact_identities
    ):
        raise StandardBuildInputError("builder received different provider artifacts")


def validate_standard_build_authority(
    plan: StandardComponentBuildPlan,
    inputs: StandardPlanFinalizationInputs,
    *,
    now: datetime,
) -> None:
    """Recheck current authorization and exact portable plan before host access."""
    if not isinstance(inputs, StandardPlanFinalizationInputs):
        raise TypeError("build authority requires typed plan finalization inputs")
    authorization = inputs.authorization
    intent = inputs.intent
    authorization.grant.require_valid(intent.build_request, now=now)
    if (
        authorization.grant.classification_digest != authorization.index_identity.uri
        or authorization.grant.privileges != intent.build_request.requested_privileges
    ):
        raise StandardBuildInputError(
            "build grant differs from its index or privileges"
        )
    expected = inputs.finalize()
    if (
        not isinstance(plan, StandardComponentBuildPlan)
        or plan.to_dict() != expected.to_dict()
    ):
        raise StandardBuildInputError(
            "remote plan differs from current local authority"
        )
