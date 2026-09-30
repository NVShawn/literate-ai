"""Reopen importing-maintainer review of an exact Cargo test inventory."""

import json
from dataclasses import dataclass, field

from literate_ai.adapters.retained_cargo_current import (
    RetainedCargoImporterAuthority,
    _unique_object,
)
from literate_ai.adapters.retained_cargo_materialization import (
    RetainedCargoMaterialization,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.retained_cargo_tests import RetainedCargoTestInventory
from literate_ai.projects import PROJECT_FILENAME, PinnedInputClosure


@dataclass(frozen=True, slots=True)
class RetainedCargoTestAuthority:
    materialized: RetainedCargoMaterialization
    importer: RetainedCargoImporterAuthority
    inventory: RetainedCargoTestInventory
    reviewed_inventory: BlobRef
    _inputs: PinnedInputClosure = field(repr=False, compare=False)

    def require_unchanged(self):
        self.importer.require_unchanged()
        self.materialized.require_unchanged()
        self._inputs.require_unchanged()
        self.importer.require_unchanged()


def read_retained_cargo_test_authority(
    materialized: RetainedCargoMaterialization,
    importer: RetainedCargoImporterAuthority,
    *,
    inventory_path: str,
    reviewed_inventory: BlobRef,
    maximum_bytes: int = 4 * 1024 * 1024,
) -> RetainedCargoTestAuthority:
    """Read reviewed case expectations; no discovery, compilation or execution."""
    if not isinstance(materialized, RetainedCargoMaterialization) or not isinstance(
        importer, RetainedCargoImporterAuthority
    ):
        raise TypeError("retained.tests.current-authority-required")
    if (
        not isinstance(reviewed_inventory, BlobRef)
        or type(maximum_bytes) is not int
        or not 0 < reviewed_inventory.size <= maximum_bytes
    ):
        raise ValueError("retained.tests.review-required")
    if (
        importer.project.root != materialized._files.project.root
        or importer.reviewed_binding_identity != materialized._files.binding.identity
        or importer.gates.commands != materialized.plan.gates
    ):
        raise ValueError("retained.tests.importer-mismatch")
    importer.require_unchanged()
    materialized.require_unchanged()
    canonical_relative_posix_paths(
        (PROJECT_FILENAME, inventory_path), label="reviewed test inventory path"
    )
    root = importer.project.root
    inputs = PinnedInputClosure(
        maximum_files=1,
        maximum_file_bytes=maximum_bytes,
        maximum_total_bytes=maximum_bytes,
    )
    content = inputs.pin(
        root / inventory_path,
        boundary=root,
        label=inventory_path,
        expected_identity=reviewed_inventory.identity,
    )
    if len(content) != reviewed_inventory.size:
        raise ValueError("retained.tests.inventory-size-mismatch")
    try:
        inventory = RetainedCargoTestInventory.from_dict(
            json.loads(content, object_pairs_hook=_unique_object)
        )
    except (ValueError, TypeError, RecursionError):
        raise ValueError("retained.tests.inventory-invalid") from None
    if (
        inventory.importer_project_id != importer.project.definition.project_id
        or inventory.workspace_plan_identity != materialized.plan.identity
        or inventory.gate_policy_identity != importer.gates.identity
    ):
        raise ValueError("retained.tests.inventory-authority-mismatch")
    inventory.require_graph(materialized.plan.graph)
    result = RetainedCargoTestAuthority(
        materialized, importer, inventory, reviewed_inventory, inputs
    )
    result.require_unchanged()
    return result
