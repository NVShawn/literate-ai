"""Join explicit importing-maintainer review to measured provider inputs."""

import re

from literate_ai.adapters.retained_cargo_import import RetainedCargoImportInputs
from literate_ai.adapters.retained_cargo_import_v2 import RetainedCargoCurrentInputsV2
from literate_ai.adapters.retained_cargo_review_v2 import RetainedCargoReviewedFilesV2
from literate_ai.adapters.retained_provider_native import RetainedProviderNative
from literate_ai.contracts.identity import ContentIdentity


def compose_retained_cargo_current_inputs_v2(
    reviewed: RetainedCargoReviewedFilesV2,
    native: RetainedProviderNative,
    *,
    reviewed_binding_identity: ContentIdentity,
    configured_store_id: str,
) -> RetainedCargoCurrentInputsV2:
    """Use live native authority, never candidate-derived review/store values.

    The retained nonserialized native input value is shared data; its v1 wire
    preflight is never called. The v2 receiver independently validates the actual
    v2 binding, policy, inventory and patch-aware plan before archive admission.
    """
    if (
        type(reviewed) is not RetainedCargoReviewedFilesV2
        or type(native) is not RetainedProviderNative
        or not isinstance(reviewed_binding_identity, ContentIdentity)
    ):
        raise TypeError("retained.importer.v2-current-authority-required")
    if (
        not isinstance(configured_store_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", configured_store_id)
        is None
    ):
        raise ValueError("retained.importer.v2-store-id-invalid")
    if reviewed.binding.identity != reviewed_binding_identity:
        raise ValueError("retained.importer.v2-independent-review-mismatch")

    def guard():
        reviewed.require_unchanged()
        native.require_unchanged()
        reviewed.require_unchanged()

    guard()
    provider = native.provider
    authority = provider.generation.prepared.locked_authority_snapshot.authority
    targets = tuple(
        t
        for t in native.toolchain.cargo_targets
        if t.component_revision == authority.lock.root_revision
    )
    if len(targets) != 1 or not targets[0].library:
        raise ValueError("retained.importer.v2-cargo-library-required")
    target = targets[0]
    if not {
        target.build_system_toolchain_identity,
        target.language_compiler_identity,
    } <= set(reviewed.policy.toolchains):
        raise ValueError("retained.importer.v2-gate-toolchains-mismatch")
    current = RetainedCargoImportInputs(
        authority,
        provider.promotion.evidence,
        reviewed.project.definition.project_id,
        reviewed_binding_identity,
        configured_store_id,
        provider.promotion.generation_closure,
        provider.qualification.case_map.verifier_identity,
        provider.lifecycle.policy.identity,
        target.build_system_toolchain_identity,
        target.language_compiler_identity,
        reviewed.policy.commands,
        native.oracle,
        provider.generation.recipes,
        native.commands,
        provider.qualification.profile,
        provider.lifecycle.driver,
        guard,
    )
    result = RetainedCargoCurrentInputsV2(reviewed, current)
    result.preflight(max_plan_bytes=256 * 1024 * 1024)
    return result
