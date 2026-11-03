"""Current-authority v2 Cargo archive verification, without materialization.

Both resolvers must reopen independent importer/provider inputs. A reviewed
document or archive capture never supplies execution, exclusion or scratch rights.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from literate_ai.adapters.qualification_archive import reopen_qualification_archive
from literate_ai.adapters.qualification_capture import (
    QualificationRunCapture,
    reopen_qualification_products,
)
from literate_ai.adapters.retained_cargo_import import RetainedCargoImportInputs
from literate_ai.adapters.retained_cargo_plan import (
    _verify_retained_cargo_package_values,
    _verify_retained_cargo_provider,
)
from literate_ai.adapters.retained_cargo_review_v2 import RetainedCargoReviewedFilesV2
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_cargo_workspace_v2 import (
    RetainedCargoWorkspacePlanV2,
)


@dataclass(frozen=True, slots=True)
class RetainedCargoCurrentInputsV2:
    """Live v2 review plus independently resolved native provider/tool/gate inputs.

    current is the existing nonserialized input value, not its v1 preflight
    protocol. Its expected binding identity and configured logical store must
    come from independent maintainer/configuration authority, never the candidate.
    Only this version-specific preflight may join it to v2 reviewed documents.
    """

    reviewed: RetainedCargoReviewedFilesV2
    current: RetainedCargoImportInputs

    def __post_init__(self):
        if (
            type(self.reviewed) is not RetainedCargoReviewedFilesV2
            or type(self.current) is not RetainedCargoImportInputs
        ):
            raise TypeError("retained.cargo.v2-current-inputs-required")

    def require_unchanged(self):
        self.reviewed.require_unchanged()
        self.current.require_unchanged()
        self.reviewed.require_unchanged()

    def preflight(self, *, max_plan_bytes: int):
        self.require_unchanged()
        reviewed, current = self.reviewed, self.current
        binding = reviewed.binding
        native = binding.common
        if (
            type(max_plan_bytes) is not int
            or max_plan_bytes < 1
            or native.workspace_plan.size > max_plan_bytes
        ):
            raise ValueError("retained.cargo.v2-plan-limit-invalid")
        if (
            not isinstance(current.reviewed_binding_identity, ContentIdentity)
            or binding.identity != current.reviewed_binding_identity
            or native.importer_project_id != current.importer_project_id
            or native.importer_project_id != reviewed.project.definition.project_id
            or native.source_store_id != current.configured_store_id
        ):
            raise ValueError("retained.cargo.importer-authority-mismatch")
        binding.require_declarations(reviewed.plan, reviewed.policy, reviewed.inventory)
        _verify_retained_cargo_provider(
            native,
            current.authority,
            current.promotion,
            current_generation_closure=current.current_generation_closure,
            current_verifier_identity=current.current_verifier_identity,
            current_policy_identity=current.current_policy_identity,
        )
        plan = reviewed.plan.common
        if (
            not isinstance(current.current_cargo_identity, ContentIdentity)
            or not isinstance(current.current_rustc_identity, ContentIdentity)
            or plan.cargo_identity != current.current_cargo_identity
            or plan.rustc_identity != current.current_rustc_identity
        ):
            raise ValueError("retained.cargo.tool-authority-mismatch")
        if (
            not isinstance(current.current_gates, tuple)
            or any(
                not isinstance(gate, RepositoryBuildCommand)
                for gate in current.current_gates
            )
            or plan.gates != current.current_gates
        ):
            raise ValueError("retained.cargo.gate-authority-mismatch")
        self.require_unchanged()
        return reviewed.plan


def verify_retained_cargo_archive_v2(
    *,
    read_current: Callable[[], RetainedCargoCurrentInputsV2],
    read_archive: Callable[[str, BlobRef, int], bytes],
    max_plan_bytes: int,
    max_archive_bytes: int,
    max_records: int,
    max_package_bytes: int,
) -> tuple[RetainedCargoWorkspacePlanV2, QualificationRunCapture]:
    """Reopen current review on both sides of native archive/product verification.

    Transport must enforce its byte allowance before allocating its response.
    This returns verified package observations for a separately authorized next
    transaction; it does not publish packages, execute tests or issue a receipt.
    """
    if any(
        type(n) is not int or n <= 0
        for n in (max_plan_bytes, max_archive_bytes, max_records, max_package_bytes)
    ):
        raise ValueError("retained.cargo.import-limit-invalid")
    if not callable(read_current) or not callable(read_archive):
        raise TypeError(
            "retained import requires explicit authority and transport readers"
        )
    current = read_current()
    if type(current) is not RetainedCargoCurrentInputsV2:
        raise TypeError("retained.cargo.v2-current-inputs-required")
    plan = current.preflight(max_plan_bytes=max_plan_bytes)
    binding = current.reviewed.binding.common
    provider = current.current
    if binding.qualification_archive.size > max_archive_bytes:
        raise ValueError("retained.cargo.archive-limit-exceeded")
    content = read_archive(
        provider.configured_store_id, binding.qualification_archive, max_archive_bytes
    )
    reader = reopen_qualification_archive(
        content,
        binding.qualification_archive,
        max_bytes=max_archive_bytes,
        max_records=max_records,
    )
    capture = reopen_qualification_products(
        reader,
        qualification_identity=binding.qualification_identity,
        run_identity=binding.run_identity,
        exports_identity=binding.exports.identity,
        max_package_bytes=max_package_bytes,
        component_lock=provider.authority.lock,
        oracle=provider.oracle,
        current_recipes=provider.current_recipes,
        current_commands=provider.current_commands,
        current_profile=provider.current_profile,
        current_driver=provider.current_driver,
    )
    if (
        capture.exports != binding.exports
        or capture.run.run_identity != binding.run_identity
    ):
        raise ValueError("retained.cargo.package-capture-mismatch")
    # Reuse semantic manifest/export checks only. The v1 plan parser and its
    # canonical byte reference are never substituted for the actual v2 documents.
    _verify_retained_cargo_package_values(
        plan.common,
        binding,
        capture,
        max_package_bytes=max_package_bytes,
        max_entries=max_records,
    )
    current.require_unchanged()
    latest = read_current()
    if type(latest) is not RetainedCargoCurrentInputsV2:
        raise TypeError("retained.cargo.v2-current-inputs-required")
    latest.preflight(max_plan_bytes=max_plan_bytes)
    if latest != current:
        raise ValueError("retained.cargo.current-inputs-changed")
    current.require_unchanged()
    return plan, capture
