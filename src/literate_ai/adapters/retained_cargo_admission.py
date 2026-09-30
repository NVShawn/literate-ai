"""Compose reviewed retirement, guarded execution and durable Cargo admission."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from literate_ai.adapters.retained_cargo_execution import (
    execute_retained_cargo_consumer,
)
from literate_ai.adapters.retained_cargo_execution_inputs import (
    RetainedCargoExecutionInputs,
)
from literate_ai.adapters.retained_cargo_files import _require_absent
from literate_ai.adapters.retained_cargo_materialization import (
    RetainedCargoMaterialization,
)
from literate_ai.adapters.retained_cargo_test_authority import (
    RetainedCargoTestAuthority,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.contracts.retained_cargo_admission import (
    RetainedCargoAdmissionReceipt,
    RetainedCargoSourceRetirement,
)
from literate_ai.projects import PinnedInputClosure

from .retained_cargo_current import _unique_object


@dataclass(frozen=True, slots=True)
class RetainedCargoRetirementAuthority:
    materialized: RetainedCargoMaterialization
    retirement: RetainedCargoSourceRetirement
    reviewed_retirement: BlobRef
    _inputs: PinnedInputClosure = field(repr=False, compare=False)

    def require_unchanged(self) -> None:
        self.materialized.require_unchanged()
        self._inputs.require_unchanged()
        root = self.materialized._files.project.root
        for relative in self.retirement.retired_roots:
            _require_absent(root, relative)
        self._inputs.require_unchanged()
        self.materialized.require_unchanged()


def read_retained_cargo_retirement_authority(
    materialized: RetainedCargoMaterialization,
    *,
    retirement_path: str,
    reviewed_retirement: BlobRef,
    maximum_bytes: int = 4 * 1024 * 1024,
) -> RetainedCargoRetirementAuthority:
    """Reopen a reviewed retirement declaration and require every root absent."""
    if not isinstance(materialized, RetainedCargoMaterialization):
        raise TypeError("retained.admission.materialization-required")
    if (
        not isinstance(reviewed_retirement, BlobRef)
        or type(maximum_bytes) is not int
        or not 0 < reviewed_retirement.size <= maximum_bytes
    ):
        raise ValueError("retained.admission.retirement-review-required")
    canonical_relative_posix_path(
        retirement_path, label="retained Cargo retirement authority"
    )
    if len(retirement_path) > 128:
        raise ValueError("retained.admission.retirement-path-too-long")
    root = materialized._files.project.root
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
        raise ValueError("retained.admission.retirement-size-mismatch")
    try:
        retirement = RetainedCargoSourceRetirement.from_dict(
            json.loads(content, object_pairs_hook=_unique_object)
        )
    except (ValueError, TypeError, RecursionError):
        raise ValueError("retained.admission.retirement-invalid") from None
    project = materialized._files.project.definition
    if (
        retirement.importer_project_id != project.project_id
        or retirement.binding_identity != materialized._files.binding.identity
    ):
        raise ValueError("retained.admission.retirement-authority-mismatch")
    destinations = tuple(
        PurePosixPath(path) for _, path in materialized._files.binding.destinations
    )
    required = {
        PurePosixPath(materialized.plan.workspace_root) / item.path
        for item in materialized.plan.manifests
        if item.after is not None
    }
    roots = tuple(PurePosixPath(path) for path in retirement.retired_roots)
    if any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for left in roots
        for right in (*destinations, *required)
    ):
        raise ValueError("retained.admission.retirement-overlap")
    result = RetainedCargoRetirementAuthority(
        materialized, retirement, reviewed_retirement, inputs
    )
    result.require_unchanged()
    return result


def admit_retained_cargo_consumer(
    materialized,
    importer,
    *,
    retirement_authority,
    test_authority,
    cargo,
    rustc,
    gate_tools,
    environment,
    consumer_inputs,
    allow_host_execution,
    acknowledge_source_retirement,
    offline=True,
    timeout_seconds=900,
) -> RetainedCargoAdmissionReceipt:
    """Issue admission only after reviewed positive tests with retired source absent."""
    if acknowledge_source_retirement is not True:
        raise ValueError("retained.admission.retirement-acknowledgement-required")
    if (
        not isinstance(retirement_authority, RetainedCargoRetirementAuthority)
        or retirement_authority.materialized is not materialized
        or not isinstance(test_authority, RetainedCargoTestAuthority)
        or test_authority.materialized is not materialized
        or test_authority.importer is not importer
        or not isinstance(consumer_inputs, RetainedCargoExecutionInputs)
        or consumer_inputs.materialized is not materialized
    ):
        raise ValueError("retained.admission.authority-mismatch")
    retirement_authority.require_unchanged()
    test_authority.require_unchanged()
    consumer_identity = consumer_inputs.current_identity()
    observations = execute_retained_cargo_consumer(
        materialized,
        importer,
        cargo=cargo,
        rustc=rustc,
        gate_tools=gate_tools,
        environment=environment,
        consumer_inputs=consumer_inputs,
        allow_host_execution=allow_host_execution,
        test_authority=test_authority,
        offline=offline,
        timeout_seconds=timeout_seconds,
    )
    retirement_authority.require_unchanged()
    test_authority.require_unchanged()
    if consumer_inputs.current_identity() != consumer_identity:
        raise ValueError("retained.admission.consumer-inputs-changed")
    expected_steps = (
        "retained-cargo-metadata",
        *(command.step_id for command in materialized.plan.gates),
        "retained-test-runtime",
        "retained-test-compile",
        *(
            f"retained-test-{index}-{phase}"
            for index in range(len(test_authority.inventory.targets))
            for phase in ("list", "run")
        ),
        "retained-cargo-metadata",
    )
    if tuple(item.command.step_id for item in observations) != expected_steps:
        raise ValueError("retained.admission.positive-tests-required")
    receipt = RetainedCargoAdmissionReceipt(
        materialized._files.binding.identity,
        materialized.plan.identity,
        importer.gates.identity,
        test_authority.inventory.identity,
        retirement_authority.retirement.identity,
        consumer_identity,
        materialized._files.binding.qualification_identity,
        tuple(item.identity for item in observations),
    )
    retirement_authority.require_unchanged()
    test_authority.require_unchanged()
    return receipt
