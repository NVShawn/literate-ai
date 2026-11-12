"""Authored-origin Cargo receiving without source-promotion placeholders.

Independent importer review and actual consumer tools remain separate from the
live authored publication owner. These read-only checks grant no write, scratch,
source-retirement or execution rights.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from literate_ai.adapters.authored_qualification_projection import (
    reopen_projected_authored_run,
)
from literate_ai.adapters.builders.cargo import CargoToolchain
from literate_ai.adapters.builders.rust import RustToolchain
from literate_ai.adapters.qualification_archive import reopen_qualification_archive
from literate_ai.adapters.retained_authored_provider import RetainedAuthoredProvider
from literate_ai.adapters.retained_cargo_plan import (
    _verify_retained_cargo_package_values,
)
from literate_ai.adapters.retained_cargo_review_v2 import RetainedCargoReviewedFilesV2
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.repositories import RepositoryBuildCommand


@dataclass(frozen=True, slots=True)
class RetainedAuthoredCargoCurrentInputsV2:
    """One live authored provider and independently admitted importer snapshot."""

    reviewed: RetainedCargoReviewedFilesV2
    provider: RetainedAuthoredProvider
    reviewed_binding_identity: ContentIdentity
    configured_store_id: str
    consumer_cargo: CargoToolchain
    consumer_rustc: RustToolchain
    current_gates: tuple[RepositoryBuildCommand, ...]

    def __post_init__(self):
        if (
            type(self.reviewed) is not RetainedCargoReviewedFilesV2
            or type(self.provider) is not RetainedAuthoredProvider
            or not isinstance(self.reviewed_binding_identity, ContentIdentity)
            or type(self.consumer_cargo) is not CargoToolchain
            or type(self.consumer_rustc) is not RustToolchain
        ):
            raise TypeError("retained.cargo.authored-current-inputs-required")
        if (
            not isinstance(self.configured_store_id, str)
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.configured_store_id
            )
            is None
        ):
            raise ValueError("retained.cargo.authored-store-id-invalid")
        if not isinstance(self.current_gates, tuple) or any(
            not isinstance(command, RepositoryBuildCommand)
            for command in self.current_gates
        ):
            raise TypeError("retained.cargo.authored-current-gates-required")

    def require_unchanged(self):
        self.reviewed.require_unchanged()
        self.provider.require_unchanged()
        self.consumer_cargo.require_unchanged()
        self.consumer_rustc.require_unchanged()
        self.reviewed.require_unchanged()

    def preflight(self, *, max_plan_bytes):
        self.require_unchanged()
        reviewed = self.reviewed
        binding = reviewed.binding.common
        if (
            type(max_plan_bytes) is not int
            or max_plan_bytes < 1
            or binding.workspace_plan.size > max_plan_bytes
        ):
            raise ValueError("retained.cargo.v2-plan-limit-invalid")
        if (
            reviewed.binding.identity != self.reviewed_binding_identity
            or binding.importer_project_id != reviewed.project.definition.project_id
            or binding.source_store_id != self.configured_store_id
        ):
            raise ValueError("retained.cargo.importer-authority-mismatch")
        reviewed.binding.require_declarations(
            reviewed.plan, reviewed.policy, reviewed.inventory
        )
        current = self.provider.current
        reference = self.provider.publication.reference
        if (
            binding.qualification_archive != reference.archive
            or binding.qualification_identity != reference.qualification_identity
            or binding.verifier_identity != current.plan.oracle_set_identity
            or binding.policy_identity != current.plan.lifecycle_policy_identity
        ):
            raise ValueError("retained.cargo.authored-provider-authority-mismatch")
        lock = current.oracles.authority.locked.authority.lock
        roots = set(binding.exports.link_plan.resolved_root_artifact_identities)
        if any(
            product.artifact_export.component_revision != lock.root_revision
            for product in binding.exports.libraries
            if product.artifact_export.identity in roots
        ):
            raise ValueError("retained.cargo.provider-revision-mismatch")
        plan = reviewed.plan.common
        cargo = ContentIdentity.parse_uri(self.consumer_cargo.identity)
        rustc = ContentIdentity.parse_uri(self.consumer_rustc.identity)
        if (
            plan.cargo_identity != cargo
            or plan.rustc_identity != rustc
            or not {cargo, rustc} <= set(reviewed.policy.toolchains)
        ):
            raise ValueError("retained.cargo.tool-authority-mismatch")
        if plan.gates != self.current_gates:
            raise ValueError("retained.cargo.gate-authority-mismatch")
        self.require_unchanged()
        return reviewed.plan


def _verify_current_authored_archive(
    current,
    *,
    read_current,
    read_archive,
    max_plan_bytes,
    max_archive_bytes,
    max_records,
    max_package_bytes,
):
    """Origin-specific implementation after the shared closed dispatch check."""
    if type(current) is not RetainedAuthoredCargoCurrentInputsV2:
        raise TypeError("retained.cargo.authored-current-inputs-required")
    plan = current.preflight(max_plan_bytes=max_plan_bytes)
    binding = current.reviewed.binding.common
    if binding.qualification_archive.size > max_archive_bytes:
        raise ValueError("retained.cargo.archive-limit-exceeded")
    content = read_archive(
        current.configured_store_id, binding.qualification_archive, max_archive_bytes
    )
    reader = reopen_qualification_archive(
        content,
        binding.qualification_archive,
        max_bytes=max_archive_bytes,
        max_records=max_records,
    )
    current.require_unchanged()
    capture = reopen_projected_authored_run(
        current.provider.projection,
        reader,
        current.provider.publication.reference.products_identity,
        run_identity=binding.run_identity,
        exports_identity=binding.exports.identity,
        max_package_bytes=max_package_bytes,
    )
    if (
        capture.exports != binding.exports
        or capture.run.request.run_identity != binding.run_identity
    ):
        raise ValueError("retained.cargo.package-capture-mismatch")
    _verify_retained_cargo_package_values(
        plan.common,
        binding,
        capture,
        max_package_bytes=max_package_bytes,
        max_entries=max_records,
    )
    current.require_unchanged()
    latest = read_current()
    if type(latest) is not RetainedAuthoredCargoCurrentInputsV2:
        raise TypeError("retained.cargo.authored-current-inputs-required")
    latest.preflight(max_plan_bytes=max_plan_bytes)
    if latest != current:
        raise ValueError("retained.cargo.current-inputs-changed")
    current.require_unchanged()
    return plan, capture


def verify_retained_authored_cargo_archive_v2(
    *,
    read_current: Callable[[], RetainedAuthoredCargoCurrentInputsV2],
    read_archive,
    max_plan_bytes,
    max_archive_bytes,
    max_records,
    max_package_bytes,
):
    """Reopen authored products only under live provider and importer custody."""
    if any(
        type(value) is not int or value <= 0
        for value in (max_plan_bytes, max_archive_bytes, max_records, max_package_bytes)
    ):
        raise ValueError("retained.cargo.import-limit-invalid")
    if not callable(read_current) or not callable(read_archive):
        raise TypeError("retained import requires explicit authority and transport readers")
    return _verify_current_authored_archive(
        read_current(),
        read_current=read_current,
        read_archive=read_archive,
        max_plan_bytes=max_plan_bytes,
        max_archive_bytes=max_archive_bytes,
        max_records=max_records,
        max_package_bytes=max_package_bytes,
    )
