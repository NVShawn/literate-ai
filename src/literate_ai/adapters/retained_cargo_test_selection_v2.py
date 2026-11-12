"""Version-matched Cargo target attribution after exact registry patch checks."""

from literate_ai.adapters.cargo_workspace_patches_v2 import (
    verify_cargo_workspace_patches_v2,
)
from literate_ai.adapters.retained_cargo_test_observation import (
    _select_cargo_test_executable_values,
)


def select_cargo_test_executables_v2(
    result,
    metadata,
    *,
    workspace_root,
    output_root,
    plan,
    workspace_manifest,
    lockfile,
    registry_sources=None,
    maximum_bytes=8 * 1024 * 1024,
    maximum_records=10000,
):
    """Reopen the real v2 graph; only then share wire-neutral attribution."""
    verify_cargo_workspace_patches_v2(
        metadata,
        workspace_root=workspace_root,
        plan=plan,
        workspace_manifest=workspace_manifest,
        lockfile=lockfile,
        registry_sources=registry_sources,
    )
    return _select_cargo_test_executable_values(
        result,
        metadata,
        workspace_root=workspace_root,
        output_root=output_root,
        expected=plan.graph.common,
        maximum_bytes=maximum_bytes,
        maximum_records=maximum_records,
    )
