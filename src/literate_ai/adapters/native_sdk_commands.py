"""Project direct SDK execution from already locked Standard Python commands."""

import base64
import json
import zlib
from dataclasses import replace

from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_RUNTIME_DRIVER,
    STANDARD_PYTHON_SDK_LIBRARY_IMPORT_DRIVER,
    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
)
from literate_ai.contracts.executable_components import (
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentLifecycleCommand,
)
from literate_ai.contracts.identity import ContentIdentity


def project_native_sdk_commands(
    contract: ComponentCommandContract,
    *,
    target_identity: ContentIdentity,
) -> ComponentCommandContract:
    """Keep build semantics; select verified imports for every runtime surface."""
    if not isinstance(contract, ComponentCommandContract):
        raise TypeError("SDK commands require a Component command contract")
    if not isinstance(target_identity, ContentIdentity):
        raise TypeError("SDK commands require an exact consumer target")
    library_surface_encoded = None
    if contract.is_library:
        if contract.library_import_surface.language != "python":
            raise ValueError("SDK library commands require a Python import surface")
        content = json.dumps(
            contract.library_import_surface.to_dict(), separators=(",", ":")
        ).encode("utf-8")
        library_surface_encoded = base64.urlsafe_b64encode(
            zlib.compress(content, level=9)
        ).decode("ascii")

    def command(original):
        if original.phase is ComponentCommandPhase.BUILD:
            return original
        if contract.is_library:
            driver = (
                STANDARD_PYTHON_SDK_RUNTIME_DRIVER
                if original.phase is ComponentCommandPhase.TEST
                else STANDARD_PYTHON_SDK_LIBRARY_IMPORT_DRIVER
            )
            arguments = (
                "{artifact_root}",
                "{export_path}",
                "tree",
                "source/main.py"
                if original.phase is ComponentCommandPhase.TEST
                else library_surface_encoded,
                "--litai-test"
                if original.phase is ComponentCommandPhase.TEST
                else "--litai-smoke",
            )
        else:
            argv = original.argv
            if len(argv) != 8 or argv[:3] != (
                "{tool}",
                "-c",
                STANDARD_PYTHON_RUNTIME_DRIVER,
            ):
                raise ValueError("SDK application commands require Standard Python")
            driver = STANDARD_PYTHON_SDK_RUNTIME_DRIVER
            arguments = argv[3:]
        return ComponentLifecycleCommand(
            original.phase,
            ("{tool}", "-I", "-B", "-c", driver, "{native_sdk_inputs}", *arguments),
        )

    entrypoints = None
    if contract.entrypoint_contracts is not None:
        entrypoints = tuple(
            replace(
                item,
                entrypoint_identity=replace(
                    item.artifact_export, target_identity=target_identity
                ).identity,
                artifact_export=replace(
                    item.artifact_export, target_identity=target_identity
                ),
                commands=tuple(command(value) for value in item.commands),
            )
            for item in contract.entrypoint_contracts
        )
    return replace(
        contract,
        commands=tuple(command(value) for value in contract.commands),
        artifact_export=replace(
            contract.artifact_export, target_identity=target_identity
        ),
        entrypoint_contracts=entrypoints,
    )
