"""Issue a v2 receipt only after real retirement and guarded full consumer tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from literate_ai.adapters.retained_cargo_current import _unique_object
from literate_ai.adapters.retained_cargo_execution_v2 import (
    execute_retained_cargo_consumer_v2,
)
from literate_ai.adapters.retained_cargo_files import _require_absent
from literate_ai.adapters.retained_cargo_materialization_v2 import (
    RetainedCargoMaterializationV2,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.contracts.retained_cargo_admission import (
    RetainedCargoAdmissionReceipt,
    RetainedCargoSourceRetirement,
)
from literate_ai.contracts.retained_cargo_admission_v2 import (
    RetainedCargoAdmissionReceiptV2,
)
from literate_ai.projects import PinnedInputClosure

_CREATION_KEY = object()


@dataclass(init=False, frozen=True, slots=True)
class RetainedCargoRetirementAuthorityV2:
    materialized: RetainedCargoMaterializationV2
    retirement: RetainedCargoSourceRetirement
    _inputs: PinnedInputClosure = field(repr=False)

    def __init__(self, *, key, materialized, retirement, inputs):
        if key is not _CREATION_KEY:
            raise TypeError("retained.admission.v2-live-retirement-required")
        for name, value in (
            ("materialized", materialized),
            ("retirement", retirement),
            ("_inputs", inputs),
        ):
            object.__setattr__(self, name, value)

    def require_unchanged(self):
        self.materialized.require_unchanged()
        self._inputs.require_unchanged()
        root = self.materialized.files.reviewed.project.root
        for relative in self.retirement.retired_roots:
            _require_absent(root, relative)
        self._inputs.require_unchanged()
        self.materialized.require_unchanged()


def read_retained_cargo_retirement_authority_v2(
    materialized: RetainedCargoMaterializationV2,
    *,
    retirement_path: str,
    reviewed_retirement: BlobRef,
    maximum_bytes: int = 4 * 1024 * 1024,
) -> RetainedCargoRetirementAuthorityV2:
    """Reopen independent reviewed retirement; never delete a source path.

    Retirement must have been authorized/performed before the receiving source
    snapshot. Changing original source during the live wrapper/scratch operation
    invalidates that operation instead of renewing its exclusions or input owner.
    The existing nonempty retirement contract and all absence checks are retained.
    """
    if type(materialized) is not RetainedCargoMaterializationV2:
        raise TypeError("retained.admission.v2-materialization-required")
    if (
        type(reviewed_retirement) is not BlobRef
        or type(maximum_bytes) is not int
        or not 0 < reviewed_retirement.size <= maximum_bytes
        or reviewed_retirement.media_type != "application/json"
    ):
        raise ValueError("retained.admission.v2-retirement-review-required")
    canonical_relative_posix_path(
        retirement_path, label="retained Cargo retirement authority"
    )
    if len(retirement_path) > 128:
        raise ValueError("retained.admission.v2-retirement-path-too-long")
    materialized.require_unchanged()
    reviewed = materialized.files.reviewed
    root = reviewed.project.root
    inputs = PinnedInputClosure(
        maximum_files=1,
        maximum_file_bytes=maximum_bytes,
        maximum_total_bytes=maximum_bytes,
    )
    content = inputs.pin(
        root / retirement_path,
        boundary=root,
        label=retirement_path,
        expected_identity=reviewed_retirement.identity,
    )
    if len(content) != reviewed_retirement.size:
        raise ValueError("retained.admission.v2-retirement-size-mismatch")
    retirement = RetainedCargoSourceRetirement.from_dict(
        json.loads(content, object_pairs_hook=_unique_object)
    )
    if (
        retirement.importer_project_id != reviewed.project.definition.project_id
        or retirement.binding_identity != reviewed.binding.identity
    ):
        raise ValueError("retained.admission.v2-retirement-authority-mismatch")
    protected = [
        PurePosixPath(path) for _, path in reviewed.binding.common.destinations
    ]
    protected.extend(
        PurePosixPath(reviewed.plan.common.workspace_root) / item.path
        for item in reviewed.plan.common.manifests
        if item.after is not None
    )
    protected.extend(
        PurePosixPath(path) for path in (*reviewed.document_paths, retirement_path)
    )
    protected.extend(
        PurePosixPath(reviewed.plan.common.workspace_root) / item.path
        for item in reviewed.plan.scratch_roots
    )
    for relative in retirement.retired_roots:
        path = PurePosixPath(relative)
        if any(
            path.is_relative_to(other) or other.is_relative_to(path)
            for other in protected
        ):
            raise ValueError("retained.admission.v2-retirement-overlap")
    result = RetainedCargoRetirementAuthorityV2(
        key=_CREATION_KEY,
        materialized=materialized,
        retirement=retirement,
        inputs=inputs,
    )
    result.require_unchanged()
    return result


def admit_retained_cargo_consumer_v2(
    materialized: RetainedCargoMaterializationV2,
    *,
    retirement_authority: RetainedCargoRetirementAuthorityV2,
    acknowledge_source_retirement: bool,
    consumer_inputs,
    **execution,
) -> RetainedCargoAdmissionReceiptV2:
    """Execute through the real v2 owner and issue the declared v2 receipt.

    There is deliberately no no-retirement mode, serialized-result importer or
    caller-supplied successful observations. Consumer-only experiments use the
    explicit execution API and cannot mint this receipt.
    """
    if acknowledge_source_retirement is not True:
        raise ValueError("retained.admission.v2-retirement-acknowledgement-required")
    if (
        type(retirement_authority) is not RetainedCargoRetirementAuthorityV2
        or retirement_authority.materialized is not materialized
    ):
        raise TypeError("retained.admission.v2-retirement-authority-required")
    retirement_authority.require_unchanged()
    before = consumer_inputs.current_identity(environment=execution["environment"])
    result = execute_retained_cargo_consumer_v2(
        materialized, consumer_inputs=consumer_inputs, **execution
    )
    retirement_authority.require_unchanged()
    if (
        before != result.consumer_inputs_identity
        or consumer_inputs.current_identity(environment=execution["environment"])
        != before
    ):
        raise ValueError("retained.admission.v2-consumer-inputs-changed")
    reviewed = materialized.files.reviewed
    receipt = RetainedCargoAdmissionReceiptV2(
        RetainedCargoAdmissionReceipt(
            binding_identity=reviewed.binding.identity,
            workspace_plan_identity=reviewed.plan.identity,
            gate_policy_identity=reviewed.policy.identity,
            test_inventory_identity=reviewed.inventory.identity,
            source_retirement_identity=retirement_authority.retirement.identity,
            consumer_inputs_identity=before,
            qualification_identity=reviewed.binding.common.qualification_identity,
            command_observation_identities=tuple(
                x.identity for x in result.observations
            ),
        ),
        reviewed.policy.input_custody.identity,
        result.target_outcomes,
    )
    receipt.require_declarations(
        reviewed.binding, reviewed.plan, reviewed.policy, reviewed.inventory
    )
    retirement_authority.require_unchanged()
    return receipt
