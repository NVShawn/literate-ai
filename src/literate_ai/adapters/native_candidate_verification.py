"""Early native-result rejection within the existing generated-candidate loop.

This check never substitutes for independent acceptance of the final package.
Its only repair feedback is a fixed category and exact failure identities.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_ARCHIVE_BYTES,
    MAX_BUILD_ARCHIVE_FILES,
)
from literate_ai.adapters.action_build_result import _capture_files
from literate_ai.adapters.component_acceptance import ComponentAcceptanceError
from literate_ai.adapters.directory_artifacts import encode_directory_export
from literate_ai.adapters.generation_preparation import PreparedLockedGeneration
from literate_ai.adapters.lifecycle.standard_cargo import StandardCargoLifecyclePorts
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    _child_process_environment,
)
from literate_ai.adapters.rust_library_acceptance import (
    RustLibraryAcceptance,
    capture_loaded_rust_library_verifier_inputs,
)
from literate_ai.adapters.rust_library_harness_execution import (
    _require_results,
    execute_rust_library_harness,
)
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.executable_components import ComponentCommandPhase
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.product_json import product_json_bytes
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardNodeFailureEvidence,
    StandardNodeFailurePhase,
)
from literate_ai.storage.cas import FileSystemCAS

_MISMATCH = "component_acceptance.rust_harness_result_mismatch"


@dataclass(frozen=True, slots=True)
class _NativeCandidateVerifier:
    prepared: PreparedLockedGeneration
    ports: StandardCargoLifecyclePorts
    oracle: RustLibraryAcceptance = field(init=False, repr=False)
    lock: ComponentLock = field(init=False, repr=False)
    lock_identity: ContentIdentity = field(init=False)
    oracle_identity: ContentIdentity = field(init=False)

    def __post_init__(self):
        prepared, ports = self.prepared, self.ports
        if (
            type(prepared) is not PreparedLockedGeneration
            or type(ports) is not StandardCargoLifecyclePorts
        ):
            raise TypeError(
                "native candidate verification needs original prepared and Cargo owners"
            )
        object.__setattr__(self, "oracle", ports.independent_acceptance_oracle)
        object.__setattr__(
            self, "lock", prepared.locked_authority_snapshot.authority.lock
        )
        object.__setattr__(self, "lock_identity", self.lock.identity)
        object.__setattr__(self, "oracle_identity", self.oracle.identity)

    def verify(self, plan, output, evidence):
        ports, oracle, lock = self.ports, self.oracle, self.lock
        root = lock.root_revision
        ports._require_full_command_scope()
        if (
            plan.component_revision != root
            or output.provenance.retained_source_identity is not None
            or output.provenance.component_lock_identity != lock.identity
            or output.provenance.application_root_revision_identity != root
            or evidence.component_revision != root
            or evidence.source_generation_identity != output.identity
            or evidence.build.source_tree_identity != output.candidate.tree_identity
        ):
            raise ValueError("native candidate source or stage authority differs")
        contract = ports._contract(root)
        surface = contract.library_import_surface
        node = next(n for n in lock.nodes if n.revision.identity == root)
        interfaces = tuple(
            sorted(
                (r.identity for r in node.revision.public_interfaces),
                key=lambda r: r.uri,
            )
        )
        if (
            type(oracle) is not RustLibraryAcceptance
            or oracle.component != node.revision.coordinate.name
            or oracle.specification_set_identity
            != node.revision.specification_set_identity
            or oracle.public_interface_identities != interfaces
            or surface is None
            or oracle.import_surface_identity != surface.identity
            or oracle.language != surface.language
            or any(
                c.capability not in {r.capability for r in surface.capabilities}
                for c in oracle.cases
            )
        ):
            raise ValueError(
                "native candidate oracle differs from exact locked authority"
            )
        build, tests, execution = ports.acceptance_stage_evidence(
            plan, evidence.generated_tests.identity, evidence.execution.identity
        )
        if (build, tests, execution) != (
            evidence.build,
            evidence.generated_tests,
            evidence.execution,
        ):
            raise ValueError("native candidate has no exact registered stages")
        export_record = ports._planned_exports[root.uri]
        if export_record not in build.exports:
            raise ValueError("native candidate has no exact root export")
        export = ports.artifact_path(export_record)
        target = ports.cargo_targets[root.uri]
        owner = ports._rust_harness_compilers
        if owner is None or not target.library:
            raise ValueError("native candidate has no original Rust compiler")
        compiler = LocalComponentToolBinding.from_observed_toolchain(
            owner.require_compiler(target)
        )
        if compiler.command != target.rustc_command:
            raise ValueError("native candidate compiler differs")
        cargo = ports.tool_bindings[contract.library_acceptance_toolchain_identity.uri]

        def authority_current():
            self.prepared.locked_authority_snapshot.require_unchanged()
            self.prepared.input_closure.require_unchanged()
            ports.build_execution_inputs(plan)
            if (
                self.prepared.locked_authority_snapshot.authority.lock.identity
                != self.lock_identity
                or ports._contract(root) != contract
                or ports.independent_acceptance_oracle is not oracle
                or oracle.identity != self.oracle_identity
                or ports.source_trees.evidence(
                    output.candidate.tree_identity
                ).source_generation_identity
                != output.identity
                or ports.build_evidence_for_test(plan, build.exports) != build
                or ports.artifact_path(export_record) != export
                or ports._planned_exports.get(root.uri) != export_record
                or ports.cargo_targets.get(root.uri) != target
                or ports._rust_harness_compilers is not owner
                or ports.tool_bindings.get(
                    contract.library_acceptance_toolchain_identity.uri
                )
                is not cargo
            ):
                raise ValueError("native candidate custody changed")
            cargo.require_unchanged()
            compiler.require_unchanged()

        authority_current()
        files, export_custody = _capture_files(export, authority_current)
        archive = encode_directory_export(
            files,
            max_bytes=MAX_BUILD_ARCHIVE_BYTES,
            max_entries=MAX_BUILD_ARCHIVE_FILES,
        )
        if (
            len(archive) != export_record.blob.size
            or "sha256:" + hashlib.sha256(archive).hexdigest()
            != export_record.blob.identity
        ):
            raise ValueError("native candidate export differs from sealed build")

        def current():
            authority_current()
            export_custody.require_unchanged()

        current()
        # The executor's private CAS retains command streams. Keep only its last
        # two bounded stream callbacks, never a native binary or provider prompt.
        last = deque(maxlen=2)

        def record(content):
            identity = ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            )
            if len(content) <= max(
                oracle.rust_harness.stdout_limit_bytes,
                oracle.rust_harness.stderr_limit_bytes,
            ):
                last.append((identity, content))
            return identity

        store_root = (
            ports.object_root / "native-candidate-verification" / output.identity.digest
        )
        stage = Path(
            tempfile.mkdtemp(
                prefix="litai-native-candidate-",
                dir=Path(tempfile.gettempdir()).resolve(strict=True),
            )
        )
        try:
            try:
                with execute_rust_library_harness(
                    oracle,
                    stage=stage,
                    export=export,
                    library_manifest=export.joinpath(
                        *PurePosixPath(target.manifest).parts
                    ),
                    library_alias=surface.package,
                    cargo=cargo,
                    rustc=compiler,
                    cases_document=product_json_bytes(
                        [
                            {
                                "case_id": c.case_id,
                                "capability": c.capability,
                                "arguments": c.arguments,
                            }
                            for c in oracle.cases
                        ]
                    ).decode(),
                    surface_document=canonical_json_bytes(surface.to_dict()).decode(),
                    environment=_child_process_environment({}),
                    require_library_current=current,
                    record_bytes=record,
                    evidence_root=store_root,
                    allow_host_execution=True,
                ):
                    current()
            except ComponentAcceptanceError as error:
                if error.code != _MISMATCH:
                    raise
                # Re-establish every live owner after rejection before classifying
                # a behavior failure. Any drift remains terminal, never repairable.
                current()
                with capture_loaded_rust_library_verifier_inputs(oracle) as inputs:
                    inputs.require_current()
                if len(last) != 2:
                    raise ValueError(
                        "native rejection has no exact result stream"
                    ) from error
                identity, raw = last[0]
                try:
                    _require_results(raw, oracle)
                except ComponentAcceptanceError as replay:
                    if replay.code != _MISMATCH:
                        raise
                else:
                    raise ValueError(
                        "native result did not reproduce its rejection"
                    ) from error
                if ports._evidence_recorder is not None:
                    ports.retain_evidence_record(identity, raw)
                document = {
                    "schema": "literate-ai/native-candidate-result-rejection@1",
                    "component_revision": root.uri,
                    "source_output_identity": output.identity.uri,
                    "source_tree_identity": output.candidate.tree_identity.uri,
                    "build_identity": build.identity.uri,
                    "artifact_identity": export_record.identity.uri,
                    "oracle_identity": oracle.identity.uri,
                    "result_identity": identity.uri,
                    "code": _MISMATCH,
                }
                subject = ports._record_evidence(document)
                if (
                    FileSystemCAS(store_root)
                    .put_bytes(canonical_json_bytes(document))
                    .digest
                    != subject.digest
                ):
                    raise ValueError("native rejection retention differs") from error
                current()
                return StandardNodeFailureEvidence(
                    root,
                    StandardNodeFailurePhase.ACCEPT,
                    _MISMATCH,
                    subject,
                    "independent native library public results differ "
                    "from the approved contract",
                )
            current()
            return None
        finally:
            shutil.rmtree(stage)


def _native_candidate_verifier(prepared, ports):
    """Opt in only the single local Rust-v2 root; never infer broader authority."""
    if (
        type(prepared) is not PreparedLockedGeneration
        or type(ports) is not StandardCargoLifecyclePorts
        or type(ports.independent_acceptance_oracle) is not RustLibraryAcceptance
        or prepared.native_sdk_inputs is not None
        or ports._command_phases != tuple(ComponentCommandPhase)
    ):
        return None
    lock = prepared.locked_authority_snapshot.authority.lock
    if len(lock.nodes) != 1 or lock.edges or len(ports.contracts) != 1:
        return None
    return _NativeCandidateVerifier(prepared, ports)


__all__ = []
