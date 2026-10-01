"""Deterministic Standard plan construction from explicit phase inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from literate_ai.application.artifact_graph import create_composite_build_request
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardComponentBuildIntent,
    StandardComponentBuildPlan,
    StandardProjectLifecycleError,
)
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ArtifactExportDeclaration,
    ArtifactMaterializationPlan,
    BuildActionRequest,
    BuildPrivilege,
    BuildSubActionKind,
    ComponentBuildManifest,
    ComponentCommandContract,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.native_sdks import native_sdk_consumer_build_identity


@dataclass(frozen=True, slots=True)
class StandardPlanFinalizationInputs:
    intent: StandardComponentBuildIntent
    authorization: StandardBuildAuthorization
    contract: ComponentCommandContract
    providers: tuple[ArtifactExport, ...]
    package_artifacts: tuple[ArtifactExport, ...]
    dependency_resolution: Literal["none", "npm", "python"]

    def finalize(self) -> StandardComponentBuildPlan:
        return finalize_standard_component_plan(
            self.intent,
            self.authorization,
            self.contract,
            self.providers,
            self.package_artifacts,
            dependency_resolution=self.dependency_resolution,
        )


def finalize_standard_component_plan(
    intent: StandardComponentBuildIntent,
    authorization: StandardBuildAuthorization,
    contract: ComponentCommandContract,
    providers: tuple[ArtifactExport, ...],
    package_artifacts: tuple[ArtifactExport, ...],
    *,
    dependency_resolution: Literal["none", "npm", "python"],
) -> StandardComponentBuildPlan:
    """Construct a plan without host paths, controller maps, or process execution."""
    if (
        dependency_resolution not in {"none", "npm", "python"}
        or contract.component_revision != intent.component_revision
        or authorization.build_intent_identity != intent.identity
        or authorization.build_request_identity != intent.build_request_identity
        or tuple(item.identity for item in providers)
        != intent.provider_artifact_identities
        or tuple(item.identity for item in package_artifacts)
        != intent.package_artifact_identities
        or intent.build_request.toolchain_digest
        != contract.language_compiler_identity.uri
        or intent.build_request.builder_id
        != native_sdk_consumer_build_identity(
            contract.locked_build_authority_identity, intent.native_sdk_input_identities
        ).uri
        or intent.build_request.allowed_outputs
        != tuple(sorted(shape.export_id for shape in contract.artifact_export_shapes()))
        or intent.build_request.requested_privileges
        != (
            ("execute-build-tools", "network-access")
            if dependency_resolution == "npm"
            else ("execute-build-tools",)
        )
    ):
        raise StandardProjectLifecycleError(
            "standard_lifecycle.plan_inputs_mismatch",
            "plan inputs differ from exact intent and authorization",
        )
    toolchain = contract.language_compiler_identity
    build_system_driver = contract.build_system_toolchain_identity
    build_system_resolver = contract.build_system_resolver_identity
    language_runtime = contract.language_runtime_identity
    dependencies = tuple(
        sorted(
            {
                item.identity.uri: item.identity
                for item in (*providers, *package_artifacts)
            }.values(),
            key=lambda item: item.uri,
        )
    )
    declarations = tuple(
        sorted(
            (
                ArtifactExportDeclaration(
                    shape.export_id,
                    intent.component_revision,
                    shape.role,
                    shape.abi_identity,
                    shape.target_identity,
                    shape.media_type,
                    shape.producer_identity,
                    intent.source_tree_identity,
                    toolchain,
                    authorization.authorization_identity,
                    dependencies,
                )
                for shape in contract.artifact_export_shapes()
            ),
            key=lambda item: item.export_id,
        )
    )
    declaration = next(
        item
        for item in declarations
        if item.export_id == contract.artifact_export.export_id
    )
    action_name = contract.identity.digest[:24]
    action = BuildActionRequest(
        action_id=f"build-{action_name}",
        dependency_artifacts=providers,
        package_dependency_artifacts=package_artifacts,
        declared_output_ids=tuple(item.export_id for item in declarations),
        output_declarations=declarations if contract.is_multi_entrypoint else (),
        **{
            name: getattr(declaration, name)
            for name in (
                "component_revision",
                "role",
                "abi_identity",
                "target_identity",
                "media_type",
                "producer_identity",
                "source_tree_identity",
                "toolchain_identity",
                "authorization_identity",
            )
        },
    )
    actions = (action,)
    ordered_actions = ((BuildSubActionKind.COMPILE, action.action_id),)
    requested_privileges = (BuildPrivilege.EXECUTE_BUILD_TOOLS,)
    if dependency_resolution != "none":
        resolve_action = BuildActionRequest(
            **{
                name: getattr(action, name)
                for name in (
                    "component_revision",
                    "role",
                    "abi_identity",
                    "target_identity",
                    "media_type",
                    "producer_identity",
                    "source_tree_identity",
                    "toolchain_identity",
                    "authorization_identity",
                    "dependency_artifacts",
                    "declared_output_ids",
                    "package_dependency_artifacts",
                    "output_declarations",
                )
            },
            action_id=f"resolve-{action_name}",
        )
        actions = tuple(
            sorted((action, resolve_action), key=lambda item: item.action_id)
        )
        ordered_actions = (
            (BuildSubActionKind.RESOLVE_DEPENDENCIES, resolve_action.action_id),
            (BuildSubActionKind.COMPILE, action.action_id),
        )
        if dependency_resolution == "npm":
            requested_privileges = (
                BuildPrivilege.EXECUTE_BUILD_TOOLS,
                BuildPrivilege.NETWORK_ACCESS,
            )
    manifest = ComponentBuildManifest(
        intent.component_revision,
        intent.source_tree_identity,
        build_system_driver,
        actions,
        (),
        declarations,
    )
    materialization = ArtifactMaterializationPlan(
        intent.source_tree_identity,
        canonical_identity({"local-execution-root": intent.identity.uri}),
        (),
        native_sdk_input_identities=intent.native_sdk_input_identities,
    )
    request = create_composite_build_request(
        manifest,
        materialization,
        build_system_resolver_identity=build_system_resolver,
        language_compiler_identity=toolchain,
        language_runtime_identity=language_runtime,
        ordered_actions=ordered_actions,
        requested_privileges=requested_privileges,
    )
    plan = StandardComponentBuildPlan(
        intent.component_revision,
        manifest,
        materialization,
        request,
        tuple(item.identity for item in providers),
        tuple(item.identity for item in package_artifacts),
    )
    return plan
