"""Deterministic build intent from admitted portable post-source inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildIntent,
    StandardProjectLifecycleError,
)
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ComponentCommandContract,
    GeneratedSourceCandidate,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.native_sdks import native_sdk_consumer_build_identity
from literate_ai.security import BuildRequest


@dataclass(frozen=True, slots=True)
class StandardBuildIntentInputs:
    generation_plan_identity: ContentIdentity
    candidate: GeneratedSourceCandidate
    contract: ComponentCommandContract
    providers: tuple[ArtifactExport, ...]
    packages: tuple[ArtifactExport, ...]
    native_sdk_inputs: tuple[ContentIdentity, ...]
    dependency_resolution: Literal["none", "npm", "python"]

    def create(self) -> StandardComponentBuildIntent:
        return create_standard_component_build_intent(
            self.generation_plan_identity,
            self.candidate,
            self.contract,
            self.providers,
            self.packages,
            self.native_sdk_inputs,
            dependency_resolution=self.dependency_resolution,
        )


def create_standard_component_build_intent(
    generation_plan_identity: ContentIdentity,
    candidate: GeneratedSourceCandidate,
    contract: ComponentCommandContract,
    providers: tuple[ArtifactExport, ...],
    packages: tuple[ArtifactExport, ...],
    native_sdk_inputs: tuple[ContentIdentity, ...],
    *,
    dependency_resolution: Literal["none", "npm", "python"],
) -> StandardComponentBuildIntent:
    """Construct an intent without source paths, process calls or mutable maps."""
    if (
        candidate.component_generation_plan_identity != generation_plan_identity
        or candidate.component_revision != contract.component_revision
        or dependency_resolution not in {"none", "npm", "python"}
    ):
        raise StandardProjectLifecycleError(
            "standard_lifecycle.intent_inputs_mismatch",
            "build-intent inputs differ from the exact source and command authority",
        )
    request = BuildRequest(
        effective_revision_digest=contract.component_revision.uri,
        source_bundle_digest=candidate.source_bundle_identity.uri,
        builder_id=native_sdk_consumer_build_identity(
            contract.locked_build_authority_identity, native_sdk_inputs
        ).uri,
        toolchain_digest=contract.language_compiler_identity.uri,
        sandbox_profile="local-explicit-host-process",
        requested_privileges=(
            ("execute-build-tools", "network-access")
            if dependency_resolution == "npm"
            else ("execute-build-tools",)
        ),
        allowed_outputs=tuple(
            sorted(shape.export_id for shape in contract.artifact_export_shapes())
        ),
    )
    return StandardComponentBuildIntent(
        contract.component_revision,
        candidate.tree_identity,
        candidate.source_bundle_identity,
        request,
        tuple(item.identity for item in providers),
        tuple(item.identity for item in packages),
        native_sdk_inputs,
    )
