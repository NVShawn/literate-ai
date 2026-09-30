"""Independent importer review and current provider inputs for Cargo admission."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.retained_cargo_import import RetainedCargoImportInputs
from literate_ai.adapters.retained_provider_native import RetainedProviderNative
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.paths import canonical_relative_posix_paths
from literate_ai.contracts.retained_libraries import RetainedLibraryGatePolicy
from literate_ai.projects import (
    PROJECT_FILENAME,
    LoadedProject,
    PinnedInputClosure,
    ProjectConfigurationStore,
)


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("retained.importer.duplicate-field")
        result[name] = value
    return result


@dataclass(frozen=True, slots=True)
class RetainedCargoImporterAuthority:
    project: LoadedProject
    reviewed_binding_identity: ContentIdentity
    configured_store_id: str
    gates: RetainedLibraryGatePolicy
    _inputs: PinnedInputClosure = field(repr=False, compare=False)

    def require_unchanged(self) -> None:
        require_safe_directory(self.project.root)
        self._inputs.require_unchanged()


def read_retained_cargo_importer_authority(
    project_root: Path,
    *,
    reviewed_binding_identity: ContentIdentity,
    configured_store_id: str,
    gate_plan_path: str,
    reviewed_gate_plan: BlobRef,
    maximum_file_bytes: int = 4 * 1024 * 1024,
) -> RetainedCargoImporterAuthority:
    """Read caller-approved gates, without opening the candidate binding/archive.

    The explicit identities and logical store are importing-maintainer inputs. The
    caller must obtain them from current reviewed configuration, not candidate
    metadata. This guards the exact reviewed policy bytes and importing project.
    Full coverage, current source and external inputs require separate custody.
    """
    if not isinstance(reviewed_binding_identity, ContentIdentity):
        raise TypeError("retained.importer.binding-review-required")
    if not isinstance(reviewed_gate_plan, BlobRef):
        raise TypeError("retained.importer.gate-review-required")
    if (
        not isinstance(configured_store_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", configured_store_id)
        is None
    ):
        raise ValueError("retained.importer.store-id-invalid")
    if type(maximum_file_bytes) is not int or maximum_file_bytes <= 0:
        raise ValueError("retained.importer.file-limit-invalid")
    if not 0 < reviewed_gate_plan.size <= maximum_file_bytes:
        raise ValueError("retained.importer.gate-size-invalid")
    canonical_relative_posix_paths(
        (PROJECT_FILENAME, gate_plan_path), label="importer authority paths"
    )
    root = Path(project_root).absolute()
    if ".." in root.parts:
        raise ValueError("retained.importer.project-path-unsafe")
    require_safe_directory(root)
    configuration = ProjectConfigurationStore(
        root, maximum_bytes=maximum_file_bytes
    ).read()
    inputs = PinnedInputClosure(
        maximum_files=2,
        maximum_file_bytes=maximum_file_bytes,
        maximum_total_bytes=maximum_file_bytes * 2,
    )
    inputs.pin(
        root / PROJECT_FILENAME,
        boundary=root,
        label=PROJECT_FILENAME,
        expected_content=configuration.content,
    )
    content = inputs.pin(
        root / gate_plan_path,
        boundary=root,
        label=gate_plan_path,
        expected_identity=reviewed_gate_plan.identity,
    )
    if len(content) != reviewed_gate_plan.size:
        raise ValueError("retained.importer.gate-size-mismatch")
    try:
        gates = RetainedLibraryGatePolicy.from_dict(
            json.loads(content, object_pairs_hook=_unique_object)
        )
    except (ValueError, TypeError, RecursionError):
        raise ValueError("retained.importer.gate-plan-invalid") from None
    if gates.importer_project_id != configuration.definition.project_id:
        raise ValueError("retained.importer.gate-project-mismatch")
    result = RetainedCargoImporterAuthority(
        LoadedProject(root, configuration.definition),
        reviewed_binding_identity,
        configured_store_id,
        gates,
        inputs,
    )
    result.require_unchanged()
    return result


def compose_retained_cargo_current_inputs(
    importer: RetainedCargoImporterAuthority,
    native: RetainedProviderNative,
) -> RetainedCargoImportInputs:
    """Use current provider measurements, never historical candidate identities.

    This composition pins no package-destination absences, so materialization can
    transition those paths under its separate authored-file/package-tree custody.
    No filesystem publication, native gate or authority transition happens here.
    """
    if not isinstance(importer, RetainedCargoImporterAuthority) or not isinstance(
        native, RetainedProviderNative
    ):
        raise TypeError("retained.importer.current-authority-required")

    def guard():
        importer.require_unchanged()
        native.require_unchanged()
        importer.require_unchanged()

    guard()
    provider = native.provider
    authority = provider.generation.prepared.locked_authority_snapshot.authority
    targets = tuple(
        t
        for t in native.toolchain.cargo_targets
        if t.component_revision == authority.lock.root_revision
    )
    if len(targets) != 1 or not targets[0].library:
        raise ValueError("retained.importer.cargo-library-required")
    target = targets[0]
    if not {
        target.build_system_toolchain_identity,
        target.language_compiler_identity,
    } <= set(importer.gates.toolchains):
        raise ValueError("retained.importer.gate-toolchains-mismatch")
    current = RetainedCargoImportInputs(
        authority,
        provider.promotion.evidence,
        importer.project.definition.project_id,
        importer.reviewed_binding_identity,
        importer.configured_store_id,
        provider.promotion.generation_closure,
        provider.qualification.case_map.verifier_identity,
        provider.lifecycle.policy.identity,
        target.build_system_toolchain_identity,
        target.language_compiler_identity,
        importer.gates.commands,
        native.oracle,
        provider.generation.recipes,
        native.commands,
        provider.qualification.profile,
        provider.lifecycle.driver,
        guard,
    )
    current.require_unchanged()
    return current
