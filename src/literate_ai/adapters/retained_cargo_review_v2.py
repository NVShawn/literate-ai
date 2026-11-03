"""Reopen exact reviewed v2 Cargo documents under current file custody.

This read-only snapshot grants no provider, scratch, execution, exclusion or
admission authority. Native verification and independent retained gate authority
must still be composed by the receiving transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.retained_cargo_files import _unique_object
from literate_ai.adapters.retained_cargo_plan import bound_cargo_packages
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.retained_cargo_binding_v2 import RetainedCargoBindingV2
from literate_ai.contracts.retained_cargo_policy import RetainedLibraryGatePolicyV2
from literate_ai.contracts.retained_cargo_tests_v2 import RetainedCargoTestInventoryV2
from literate_ai.contracts.retained_cargo_workspace_v2 import (
    RetainedCargoWorkspacePlanV2,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    LoadedProject,
    PinnedInputClosure,
    ProjectConfigurationStore,
)


@dataclass(frozen=True, slots=True)
class RetainedCargoReviewedFilesV2:
    project: LoadedProject
    binding: RetainedCargoBindingV2
    plan: RetainedCargoWorkspacePlanV2
    policy: RetainedLibraryGatePolicyV2
    inventory: RetainedCargoTestInventoryV2
    document_paths: tuple[str, ...]
    _inputs: PinnedInputClosure = field(repr=False, compare=False)

    def require_unchanged(self):
        require_safe_directory(self.project.root)
        self._inputs.require_unchanged()

    @property
    def identity(self):
        self.require_unchanged()
        return canonical_identity(
            {
                "schema": "literate-ai/retained-cargo-reviewed-files@2",
                "project_id": self.project.definition.project_id,
                "binding": self.binding.identity.uri,
                "plan": self.plan.identity.uri,
                "policy": self.policy.identity.uri,
                "inventory": self.inventory.identity.uri,
                "document_paths": list(self.document_paths),
                "files": self._inputs.identity,
            }
        )


def read_retained_cargo_review_v2(
    project_root: Path,
    *,
    binding_path: str,
    reviewed_binding: BlobRef,
    plan_path: str,
    gate_policy_path: str,
    test_inventory_path: str,
    maximum_file_bytes: int = 256 * 1024 * 1024,
    maximum_total_bytes: int = 1024 * 1024 * 1024,
) -> RetainedCargoReviewedFilesV2:
    """Start from the importing maintainer's explicit reviewed binding reference.

    Do not derive reviewed_binding from the candidate file. Paths are operator
    coordinates; references inside the reviewed binding select the exact bytes.
    Manifest before/after states and scratch ownership need separate snapshots.
    """
    if (
        type(maximum_file_bytes) is not int
        or type(maximum_total_bytes) is not int
        or not 0 < maximum_file_bytes <= 256 * 1024 * 1024
        or not maximum_file_bytes <= maximum_total_bytes <= 1024 * 1024 * 1024
    ):
        raise ValueError("retained.cargo.v2-review-limits-invalid")
    paths = (
        PROJECT_FILENAME,
        binding_path,
        plan_path,
        gate_policy_path,
        test_inventory_path,
    )
    canonical_relative_posix_paths(paths, label="reviewed Cargo v2 documents")
    if any(len(path) > 128 for path in paths):
        raise ValueError("retained.cargo.v2-review-path-too-long")
    root = Path(project_root).absolute()
    if ".." in root.parts:
        raise ValueError("retained.cargo.project-path-unsafe")
    require_safe_directory(root)
    inputs = PinnedInputClosure(
        maximum_files=5,
        maximum_file_bytes=maximum_file_bytes,
        maximum_total_bytes=maximum_total_bytes,
    )
    configuration = ProjectConfigurationStore(
        root, maximum_bytes=maximum_file_bytes
    ).read()
    inputs.pin(
        root / PROJECT_FILENAME,
        boundary=root,
        label=PROJECT_FILENAME,
        expected_content=configuration.content,
    )

    def read(relative, reference, parser, *, canonical=False):
        if (
            type(reference) is not BlobRef
            or reference.media_type != "application/json"
            or not 0 < reference.size <= maximum_file_bytes
        ):
            raise ValueError("retained.cargo.v2-review-reference-invalid")
        content = inputs.pin(
            root / relative,
            boundary=root,
            label=relative,
            expected_identity=reference.identity,
        )
        if len(content) != reference.size:
            raise ValueError("retained.cargo.v2-review-size-mismatch")
        try:
            document = json.loads(content, object_pairs_hook=_unique_object)
            if canonical and canonical_json_bytes(document) != content:
                raise ValueError("noncanonical")
            return parser(document)
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise ValueError("retained.cargo.v2-review-document-invalid") from None

    binding = read(binding_path, reviewed_binding, RetainedCargoBindingV2.from_dict)
    if binding.common.importer_project_id != configuration.definition.project_id:
        raise ValueError("retained.cargo.importer-project-mismatch")
    plan = read(
        plan_path,
        binding.common.workspace_plan,
        RetainedCargoWorkspacePlanV2.from_dict,
        canonical=True,
    )
    policy = read(
        gate_policy_path, binding.gate_policy, RetainedLibraryGatePolicyV2.from_dict
    )
    inventory = read(
        test_inventory_path,
        binding.test_inventory,
        RetainedCargoTestInventoryV2.from_dict,
    )
    binding.require_declarations(plan, policy, inventory)
    # Pure existing export attribution is reusable; no v1 parser, byte reference
    # or admission identity is substituted for the v2 reviewed documents.
    bound_cargo_packages(plan.common, binding.common)
    output = str(
        PurePosixPath(plan.common.workspace_root) / plan.graph.common.output_directory
    )
    canonical_relative_posix_paths(
        (*[path for _, path in binding.common.destinations], output),
        label="disjoint Cargo packages and output",
    )
    result = RetainedCargoReviewedFilesV2(
        LoadedProject(root, configuration.definition),
        binding,
        plan,
        policy,
        inventory,
        paths,
        inputs,
    )
    result.require_unchanged()
    return result
