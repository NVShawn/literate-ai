"""Shared fixtures extracted from ``tests.unit.test_standard_toolchain_observations``."""








from literate_ai.adapters.standard_toolchain_observations import (
    StandardToolObservation,
)

from literate_ai.contracts import canonical_identity

def observation(role="python"):
    return StandardToolObservation(
        role,
        canonical_identity({"tool": role}),
        ("/worker/tools/" + role,),
        (("TOOL_PATH", "/worker/lib"),),
        "3.14.0",
        (3, 14, 0),
    )

