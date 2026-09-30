"""Bind the independent library verifier separately from application commands."""

import hashlib

from literate_ai.adapters.component_acceptance import LibraryAcceptance
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_SDK_RUNTIME_DRIVER,
)
from literate_ai.contracts.executable_components.commands import (
    ComponentCommandContract,
)
from literate_ai.contracts.identity import canonical_identity


def library_acceptance_command(contract, oracle):
    """Project exact verifier authority shared by launch and retained verification."""
    if (
        not isinstance(contract, ComponentCommandContract)
        or not isinstance(oracle, LibraryAcceptance)
        or not contract.is_library
        or oracle.language != "python"
        or contract.library_import_surface.language != oracle.language
        or contract.library_import_surface.identity != oracle.import_surface_identity
        or contract.library_acceptance_toolchain_identity is None
        or hashlib.sha256(oracle.harness_content).hexdigest()
        != oracle.harness_identity.digest
    ):
        raise ValueError(
            "native SDK library acceptance requires exact Python authority"
        )
    return {
        "schema": "literate-ai/native-sdk-library-command@1",
        "oracle_identity": oracle.identity.to_dict(),
        "harness_identity": oracle.harness_identity.to_dict(),
        "import_surface_identity": oracle.import_surface_identity.to_dict(),
        "toolchain_identity": contract.library_acceptance_toolchain_identity.to_dict(),
        "driver_identity": canonical_identity(
            STANDARD_PYTHON_SDK_RUNTIME_DRIVER
        ).to_dict(),
    }
