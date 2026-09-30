"""Compose current importer preflight and exact archive verification without writes."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from literate_ai.adapters.component_acceptance import LibraryAcceptance
from literate_ai.adapters.qualification_archive import reopen_qualification_archive
from literate_ai.adapters.qualification_capture import (
    QualificationRunCapture,
    reopen_qualification_products,
)
from literate_ai.adapters.retained_cargo_plan import (
    verify_retained_cargo_inputs,
    verify_retained_cargo_package_manifests,
)
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)
from literate_ai.contracts.authority import ComponentGenerationClosure
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_cargo import RetainedCargoWorkspacePlan
from literate_ai.contracts.retained_libraries import RetainedLibraryBinding
from literate_ai.source_to_specification.promotion_materialization import (
    VerifiedSourcePromotionEvidence,
)


@dataclass(frozen=True, slots=True)
class RetainedCargoImportInputs:
    """One independently resolved input snapshot, with mandatory filesystem custody.

    The resolver must read current importer review/configuration, provider locks,
    promotion and Standard authority. It must not derive these expectations from
    the candidate binding. The guard covers every filesystem input read by that
    resolution, including native tools and retained gate authority.
    """

    authority: LockedGenerationAuthority
    promotion: VerifiedSourcePromotionEvidence
    importer_project_id: str
    reviewed_binding_identity: ContentIdentity
    configured_store_id: str
    current_generation_closure: ComponentGenerationClosure
    current_verifier_identity: ContentIdentity
    current_policy_identity: ContentIdentity
    current_cargo_identity: ContentIdentity
    current_rustc_identity: ContentIdentity
    current_gates: tuple[RepositoryBuildCommand, ...]
    oracle: LibraryAcceptance
    current_recipes: Mapping[ContentIdentity, object]
    current_commands: Mapping[ContentIdentity, object]
    current_profile: object
    current_driver: object
    require_unchanged: Callable[[], None] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not callable(self.require_unchanged):
            raise TypeError("retained import requires an input custody guard")
        for name in ("current_recipes", "current_commands"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(
                    "retained import requires current recipe and command maps"
                )
            object.__setattr__(self, name, MappingProxyType(dict(value)))

    def preflight(
        self, content: bytes, binding: RetainedLibraryBinding, *, max_bytes: int
    ) -> RetainedCargoWorkspacePlan:
        return verify_retained_cargo_inputs(
            content,
            binding,
            self.authority,
            self.promotion,
            importer_project_id=self.importer_project_id,
            reviewed_binding_identity=self.reviewed_binding_identity,
            configured_store_id=self.configured_store_id,
            current_generation_closure=self.current_generation_closure,
            current_verifier_identity=self.current_verifier_identity,
            current_policy_identity=self.current_policy_identity,
            current_cargo_identity=self.current_cargo_identity,
            current_rustc_identity=self.current_rustc_identity,
            current_gates=self.current_gates,
            max_bytes=max_bytes,
        )


def verify_retained_cargo_archive(
    binding: RetainedLibraryBinding,
    plan_content: bytes,
    *,
    read_current: Callable[[], RetainedCargoImportInputs],
    read_archive: Callable[[str, BlobRef, int], bytes],
    max_plan_bytes: int,
    max_archive_bytes: int,
    max_records: int,
    max_package_bytes: int,
) -> tuple[RetainedCargoWorkspacePlan, QualificationRunCapture]:
    """Return exact products only after both current-authority observations agree.

    The explicit transport must enforce its supplied byte limit while reading and
    resolve only the named configured store. The final result is immutable evidence
    for the next transaction, not a durable admission receipt. Materialization and
    consumer execution still require current custody, manifest/lock verification
    and full native gates. This operation creates no files or authority transitions.
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
    if not isinstance(current, RetainedCargoImportInputs):
        raise TypeError("retained import requires a typed current input snapshot")
    current.require_unchanged()
    plan = current.preflight(plan_content, binding, max_bytes=max_plan_bytes)
    # Refuse oversized references before the transport is invoked.
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
    capture = reopen_qualification_products(
        reader,
        qualification_identity=binding.qualification_identity,
        run_identity=binding.run_identity,
        exports_identity=binding.exports.identity,
        max_package_bytes=max_package_bytes,
        component_lock=current.authority.lock,
        oracle=current.oracle,
        current_recipes=current.current_recipes,
        current_commands=current.current_commands,
        current_profile=current.current_profile,
        current_driver=current.current_driver,
    )
    verify_retained_cargo_package_manifests(
        plan,
        binding,
        capture,
        max_package_bytes=max_package_bytes,
        max_entries=max_records,
    )
    current.require_unchanged()
    latest = read_current()
    if not isinstance(latest, RetainedCargoImportInputs):
        raise TypeError("retained import requires a typed current input snapshot")
    latest.require_unchanged()
    latest.preflight(plan_content, binding, max_bytes=max_plan_bytes)
    if latest != current:
        raise ValueError("retained.cargo.current-inputs-changed")
    current.require_unchanged()
    return plan, capture
