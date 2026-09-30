"""Reopen exact reviewed Cargo plan bytes; never run or materialize a package."""

import hashlib
import json
import tomllib
from pathlib import PurePosixPath

from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.adapters.qualification_capture import QualificationRunCapture
from literate_ai.application.locked_generation_authority import (
    LockedGenerationAuthority,
)
from literate_ai.application.source_promotion import (
    verify_qualified_locked_source_promotion,
)
from literate_ai.contracts.authority import ComponentGenerationClosure
from literate_ai.contracts.cargo_workspace import CargoPackageExpectation
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_cargo import RetainedCargoWorkspacePlan
from literate_ai.contracts.retained_libraries import RetainedLibraryBinding
from literate_ai.source_to_specification.promotion_materialization import (
    VerifiedSourcePromotionEvidence,
)


def verify_retained_cargo_inputs(
    content: bytes,
    binding: RetainedLibraryBinding,
    authority: LockedGenerationAuthority,
    promotion: VerifiedSourcePromotionEvidence,
    *,
    importer_project_id: str,
    reviewed_binding_identity: ContentIdentity,
    configured_store_id: str,
    current_generation_closure: ComponentGenerationClosure,
    current_verifier_identity: ContentIdentity,
    current_policy_identity: ContentIdentity,
    current_cargo_identity: ContentIdentity,
    current_rustc_identity: ContentIdentity,
    current_gates: tuple[RepositoryBuildCommand, ...],
    max_bytes: int,
) -> RetainedCargoWorkspacePlan:
    """Check independently reopened importer/provider inputs before artifact access.

    The binding identity must come from the importing maintainer's reviewed
    authority, not from the candidate document itself. Current arguments must be
    independently resolved under filesystem custody and rechecked after artifact
    reads. This preflight neither verifies an archive nor admits consumption.
    """
    if (
        not isinstance(binding, RetainedLibraryBinding)
        or not isinstance(reviewed_binding_identity, ContentIdentity)
        or binding.identity != reviewed_binding_identity
        or binding.importer_project_id != importer_project_id
        or binding.source_store_id != configured_store_id
    ):
        raise ValueError("retained.cargo.importer-authority-mismatch")
    projection = verify_qualified_locked_source_promotion(
        authority,
        promotion,
        current_generation_closure=current_generation_closure,
        current_verifier_identity=current_verifier_identity,
        current_policy_identity=current_policy_identity,
    )
    if (
        binding.qualification_identity
        != current_generation_closure.qualification_evidence_identity
        or binding.verifier_identity != current_verifier_identity
        or binding.policy_identity != current_policy_identity
    ):
        raise ValueError("retained.cargo.provider-authority-mismatch")
    qualification = promotion.qualification_lifecycle_result
    if qualification is None or not any(
        r.run_identity == binding.run_identity for r in qualification.runs
    ):
        raise ValueError("retained.cargo.provider-run-mismatch")
    roots = set(binding.exports.link_plan.resolved_root_artifact_identities)
    if any(
        p.artifact_export.component_revision != projection.component_revision_identity
        for p in binding.exports.libraries
        if p.artifact_export.identity in roots
    ):
        raise ValueError("retained.cargo.provider-revision-mismatch")
    plan = reopen_retained_cargo_plan(content, binding, max_bytes=max_bytes)
    if (
        not isinstance(current_cargo_identity, ContentIdentity)
        or not isinstance(current_rustc_identity, ContentIdentity)
        or plan.cargo_identity != current_cargo_identity
        or plan.rustc_identity != current_rustc_identity
    ):
        raise ValueError("retained.cargo.tool-authority-mismatch")
    if (
        not isinstance(current_gates, tuple)
        or any(not isinstance(g, RepositoryBuildCommand) for g in current_gates)
        or plan.gates != current_gates
    ):
        raise ValueError("retained.cargo.gate-authority-mismatch")
    return plan


def reopen_retained_cargo_plan(
    content: bytes, binding: RetainedLibraryBinding, *, max_bytes: int
) -> RetainedCargoWorkspacePlan:
    """Match pinned bytes and library destinations before later current admission.

    Transport must enforce the same bound before allocating bytes. This function
    does not establish maintainer trust, current provider qualification, toolchain
    compatibility, manifest custody or execution of the existing full gates.
    """
    if (
        type(max_bytes) is not int
        or max_bytes < 1
        or not isinstance(content, bytes)
        or not isinstance(binding, RetainedLibraryBinding)
        or len(content) > max_bytes
        or len(content) != binding.workspace_plan.size
        or hashlib.sha256(content).hexdigest() != binding.workspace_plan.digest
    ):
        raise ValueError("retained.cargo.plan-bytes-refused")
    try:
        data = json.loads(content)
        if canonical_json_bytes(data) != content:
            raise ValueError("noncanonical")
        plan = RetainedCargoWorkspacePlan.from_dict(data)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError("retained.cargo.plan-invalid") from None
    destinations = dict(binding.destinations)
    output = str(
        PurePosixPath(plan.workspace_root) / plan.graph.output_directory
    ).casefold()
    for destination in destinations.values():
        path = destination.casefold()
        if (
            path == output
            or path.startswith(output + "/")
            or output.startswith(path + "/")
        ):
            raise ValueError("retained.cargo.output-package-overlap")
    bound_cargo_packages(plan, binding)
    return plan


def bound_cargo_packages(
    plan: RetainedCargoWorkspacePlan,
    binding: RetainedLibraryBinding,
) -> dict[ContentIdentity, CargoPackageExpectation]:
    """Select reviewed Cargo roots by their library targets within exact exports.

    Cargo package names and Rust import names are distinct. The actual package
    name/version and manifest bytes are checked after immutable package reopening.
    """
    destinations = dict(binding.destinations)
    result = {}
    for product in binding.exports.libraries:
        artifact = product.artifact_export.identity
        destination = PurePosixPath(destinations[artifact])
        candidates = [
            package
            for package in plan.graph.packages
            if (PurePosixPath(plan.workspace_root) / package.root).is_relative_to(
                destination
            )
            and any(
                target.name == product.import_surface.package
                and {"lib", "rlib", "dylib"}.intersection(target.crate_types)
                for target in package.targets
            )
        ]
        if product.import_surface.language != "rust" or len(candidates) != 1:
            raise ValueError("retained.cargo.bound-library-mismatch")
        result[artifact] = candidates[0]
    return result


def verify_retained_cargo_package_manifests(
    plan: RetainedCargoWorkspacePlan,
    binding: RetainedLibraryBinding,
    capture: QualificationRunCapture,
    *,
    max_package_bytes: int,
    max_entries: int,
) -> None:
    """Bind reviewed native manifests to immutable qualified package bytes.

    Call after current-authority archive verification and before materialization.
    This performs no writes or execution and grants no importer trust. Actual
    Cargo metadata and full consumer gates still have to verify the native graph.
    """
    if any(type(n) is not int or n < 1 for n in (max_package_bytes, max_entries)):
        raise ValueError("retained.cargo.package-limits-invalid")
    if (
        not isinstance(plan, RetainedCargoWorkspacePlan)
        or not isinstance(binding, RetainedLibraryBinding)
        or not isinstance(capture, QualificationRunCapture)
        or capture.exports != binding.exports
        or capture.run.run_identity != binding.run_identity
    ):
        raise ValueError("retained.cargo.package-capture-mismatch")
    content = canonical_json_bytes(plan.to_dict())
    reopen_retained_cargo_plan(content, binding, max_bytes=len(content))
    packages = bound_cargo_packages(plan, binding)
    destinations = dict(binding.destinations)
    manifests = {item.path: item for item in plan.manifests}
    blobs = dict(capture.blobs)
    remaining_bytes, remaining_entries = max_package_bytes, max_entries
    for product in binding.exports.libraries:
        artifact = product.artifact_export.identity
        package = packages[artifact]
        destination = PurePosixPath(destinations[artifact])
        root = PurePosixPath(plan.workspace_root) / package.root
        relative = root.relative_to(destination) / "Cargo.toml"
        try:
            reference = product.artifact_export.blob
            files = read_directory_export(
                blobs[reference],
                reference,
                max_bytes=remaining_bytes,
                max_entries=remaining_entries,
            )
            remaining_bytes -= reference.size
            remaining_entries -= len(files)
            contents = {item.path: item.content for item in files}
            content = contents[str(relative)]
            reviewed = manifests[str(PurePosixPath(package.root) / "Cargo.toml")].after
            if (
                reviewed is None
                or reviewed.size != len(content)
                or reviewed.digest != hashlib.sha256(content).hexdigest()
            ):
                raise ValueError("manifest differs from reviewed bytes")
            document = tomllib.loads(content.decode("utf-8"))
            native = document["package"]
            library = document.get("lib", {})
            if (
                native["name"] != package.name
                or native["version"] != package.version
                or "workspace" in native
                or library.get("name", package.name.replace("-", "_"))
                != product.import_surface.package
            ):
                raise ValueError("manifest differs from reviewed package or library")
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            raise ValueError("retained.cargo.package-manifest-mismatch") from None
