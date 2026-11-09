"""Inert native Cargo joins to independently supplied current lock authority."""

from __future__ import annotations

from literate_ai.adapters.lifecycle.native_cargo_inputs import (
    bind_native_cargo_inputs,
    validate_native_cargo_provider_locks,
)
from literate_ai.adapters.lifecycle.standard_cargo import StandardCargoTarget
from literate_ai.adapters.lifecycle.standard_local import (
    project_library_consumer_bindings,
)
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.executable_components import ComponentCommandContract
from literate_ai.contracts.identity import ContentIdentity


def reopen_native_cargo_inputs(
    reader, *, plan, component_lock, current_commands, provider_exports, target
):
    """Reproject exact retained values; never reopen historical filesystem paths."""
    from literate_ai.contracts import ComponentLock

    if (
        type(component_lock) is not ComponentLock
        or current_commands is None
        or provider_exports is None
        or set(current_commands)
        != {node.revision.identity for node in component_lock.nodes}
        or any(
            type(contract) is not ComponentCommandContract
            or contract.component_revision != revision
            for revision, contract in current_commands.items()
        )
        or plan.component_revision not in current_commands
    ):
        raise ValueError("cargo.replay.current_authority")
    commands = {
        revision.uri: contract for revision, contract in current_commands.items()
    }
    consumer = commands[plan.component_revision.uri]
    if (
        target.component_revision != plan.component_revision
        or target.identity != consumer.locked_build_authority_identity
        or plan.request.build_system_resolver_identity
        != target.build_system_resolver_identity
        or plan.request.build_system_toolchain_identity
        != target.build_system_toolchain_identity
        or plan.request.language_compiler_identity != target.language_compiler_identity
    ):
        raise ValueError("cargo.replay.current_target")
    exports = {identity.uri: export for identity, export in provider_exports.items()}
    if any(identity != export.identity.uri for identity, export in exports.items()):
        raise ValueError("cargo.replay.exports")
    providers = tuple(
        exports[identity.uri] for identity in plan.provider_artifact_identities
    )
    targets = {}
    for revision, contract in commands.items():
        if (
            not contract.is_library
            or contract.library_import_surface.language != "rust"
        ):
            continue
        document = reader.read_json(contract.locked_build_authority_identity)
        identity = ContentIdentity.parse_uri
        native_target = StandardCargoTarget(
            component_revision=identity(document["component_revision"]),
            build_system_resolver_identity=identity(
                document["build_system_resolver_identity"]
            ),
            build_system_toolchain_identity=identity(
                document["build_system_toolchain_identity"]
            ),
            language_compiler_identity=identity(document["language_compiler_identity"]),
            manifest=document["manifest"],
            binary=document["binary"],
            rustc_command=tuple(document["rustc_command"]),
            library=document.get("library", False),
        )
        if (
            not native_target.library
            or native_target.component_revision.uri != revision
            or native_target.identity != contract.locked_build_authority_identity
            or native_target.identity_document() != document
        ):
            raise ValueError("cargo.replay.provider_target")
        targets[revision] = native_target.manifest
    bindings = project_library_consumer_bindings(
        consumer_revision=plan.component_revision,
        direct_generation_edges=tuple(
            edge
            for edge in component_lock.edges
            if edge.consumer_revision == plan.component_revision
            and edge.kind == DependencyKind.GENERATION
        ),
        target_identity=consumer.artifact_export.target_identity,
        providers=providers,
        contracts=commands,
    )
    native = bind_native_cargo_inputs(
        consumer_revision=plan.component_revision,
        target_identity=consumer.artifact_export.target_identity,
        providers=providers,
        available_exports=exports,
        contracts=commands,
        manifest_paths=targets,
        current_bindings=bindings,
        read_blob=lambda blob: reader.read_bytes(
            ContentIdentity.parse_uri("sha256:" + blob.digest)
        ),
    )
    if native is not None:
        validate_native_cargo_provider_locks(graph=native[0], sources=native[1])
    return native
