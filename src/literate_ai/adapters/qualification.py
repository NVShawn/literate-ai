"""Filesystem composition for lifecycle-backed regenerative qualification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
)
from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.component_acceptance import (
    ComponentAcceptanceError,
    LibraryAcceptance,
    load_library_acceptance,
    oracle_path,
    root_component_name,
)
from literate_ai.adapters.generation_preparation import PreparedLockedGeneration
from literate_ai.adapters.lifecycle.standard_local import (
    LocalIndependentAcceptanceCase,
    LocalIndependentAcceptanceOracle,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.qualification_authority import (
    qualification_verifier_case_map,
    qualification_verifier_record,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    QualificationRunCapture,
    capture_lifecycle_records,
    capture_qualification_run,
    capture_qualification_source_records,
    verify_qualification_generated_suite,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    ResolvedStandardProjectLifecycleDriver,
    StandardLifecycleBindingError,
)
from literate_ai.adapters.standard_rebuild import (
    FilesystemStandardRebuildRequest,
    assemble_filesystem_standard_rebuild_adapter,
)
from literate_ai.application.component_generation_scheduling import (
    ComponentInvalidationDecision,
)
from literate_ai.contracts import (
    ComponentLock,
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.library_products import LibraryArtifactProduct
from literate_ai.contracts.retained_libraries import RetainedLibraryExportSet
from literate_ai.projects import LoadedProject
from literate_ai.source_to_specification.contracts import (
    canonical_digest,
    canonical_value,
)
from literate_ai.source_to_specification.host_qualification import (
    LocalQualificationProfile,
)
from literate_ai.source_to_specification.inventory import inventory_source
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleExecution,
    QualificationLifecyclePlan,
    QualificationLifecycleRequest,
    QualificationLifecycleResult,
    QualificationLifecycleRunner,
    QualificationParityCaseEvidence,
    QualificationParityEvidence,
    QualificationVerifierCaseMap,
    QualificationWorkspaceAllocation,
    parse_qualification_json_result,
)
from literate_ai.storage import FileSystemCAS

from .standard_rebuild import FilesystemStandardRebuildError


class FilesystemQualificationError(RuntimeError):
    """Stable filesystem qualification failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _json_argument(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _expand(
    command: Sequence[str], *, workspace: Path, build_root: Path
) -> tuple[str, ...]:
    return tuple(
        item.replace("{workspace}", str(workspace)).replace(
            "{build_root}", str(build_root)
        )
        for item in command
    )


def _observe(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
    maximum_output_bytes: int,
    identity_command: Sequence[str] | None = None,
    recorder: QualificationEvidenceRecorder | None = None,
) -> tuple[object | None, ContentIdentity, bool]:
    try:
        process = run_with_tree_kill(
            tuple(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FilesystemQualificationError(
            "qualification.parity_command_failed",
            "independent parity command could not be executed within its bound",
        ) from exc
    if (
        len(process.stdout) > maximum_output_bytes
        or len(process.stderr) > maximum_output_bytes
    ):
        raise FilesystemQualificationError(
            "qualification.parity_output_limit",
            "independent parity output exceeded the configured byte bound",
        )
    value: object | None = None
    valid = process.returncode == 0
    if valid:
        try:
            value = parse_qualification_json_result(process.stdout)
        except (TypeError, ValueError, RecursionError):
            value = None
            valid = False
    document = {
        "schema": "literate-ai/qualification-process-observation@1",
        "command": list(command if identity_command is None else identity_command),
        "exit_status": process.returncode,
        "stdout": "sha256:" + hashlib.sha256(process.stdout).hexdigest(),
        "stderr": "sha256:" + hashlib.sha256(process.stderr).hexdigest(),
        "json_output_valid": valid,
    }
    observation = canonical_identity(document)
    if recorder is not None:
        recorder.remember_bytes(process.stdout)
        recorder.remember_bytes(process.stderr)
        recorder.remember_json(document)
    return value, observation, valid


class FilesystemQualificationWorkspaceAllocator:
    """Allocate one fresh outer tree per clean run and retain path custody locally."""

    def __init__(self, scratch_root: Path) -> None:
        supplied = Path(scratch_root)
        if supplied.is_symlink():
            raise FilesystemQualificationError(
                "qualification.scratch_root_invalid",
                "qualification scratch root must be a direct existing directory",
            )
        root = supplied.resolve(strict=True)
        if not root.is_dir():
            raise FilesystemQualificationError(
                "qualification.scratch_root_invalid",
                "qualification scratch root must be a direct existing directory",
            )
        self.session_root = Path(
            tempfile.mkdtemp(prefix="litai-qualification-", dir=root)
        ).resolve(strict=True)
        self.session_identity = canonical_identity(
            {
                "schema": "literate-ai/qualification-workspace-session@1",
                "nonce": self.session_root.name,
            }
        )
        self._paths: dict[str, Path] = {}

    def allocate(
        self, run_identity: ContentIdentity
    ) -> QualificationWorkspaceAllocation:
        path = self.session_root / run_identity.digest
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise FilesystemQualificationError(
                "qualification.workspace_allocation_failed",
                "clean qualification workspace could not be allocated",
            ) from exc
        if any(path.iterdir()):
            raise FilesystemQualificationError(
                "qualification.workspace_not_empty",
                "new qualification workspace was not empty",
            )
        identity = canonical_identity(
            {
                "schema": "literate-ai/qualification-workspace-allocation@1",
                "session_identity": self.session_identity.uri,
                "run_identity": run_identity.uri,
            }
        )
        self._paths[identity.uri] = path
        return QualificationWorkspaceAllocation(identity, True)

    def resolve(self, allocation_identity: ContentIdentity) -> Path:
        try:
            path = self._paths[allocation_identity.uri]
        except KeyError as exc:
            raise FilesystemQualificationError(
                "qualification.workspace_unknown",
                "lifecycle requested an unknown qualification workspace",
            ) from exc
        return path.resolve(strict=True)


class FilesystemQualificationAcceptanceOracle:
    """Project acceptance oracle pinned to reviewed qualification cases."""

    def __init__(self, profile: LocalQualificationProfile, lock: ComponentLock) -> None:
        self._lock_identity = lock.identity
        self._cases = tuple(
            LocalIndependentAcceptanceCase.create(
                case.case_id,
                list(case.arguments),
                case.expected_result,
            )
            for case in profile.cases
        )
        self._identity = canonical_identity(
            {
                "provider": "filesystem-qualification-acceptance-oracle@1",
                "profile_identity": profile.identity,
                "component_lock_identity": lock.identity.uri,
                "case_identities": [item.identity.uri for item in self._cases],
            }
        )

    @property
    def identity(self) -> ContentIdentity:
        return self._identity

    def cases(
        self, component_lock: ComponentLock
    ) -> tuple[LocalIndependentAcceptanceCase, ...]:
        if component_lock.identity != self._lock_identity:
            raise FilesystemQualificationError(
                "qualification.acceptance_lock_mismatch",
                "independent acceptance requested cases for another Component lock",
            )
        return self._cases


def _qualification_acceptance_oracle(
    project: LoadedProject,
    prepared: PreparedLockedGeneration,
    profile: LocalQualificationProfile,
    *,
    read_bytes: Callable[[Path], bytes] | None = None,
) -> LocalIndependentAcceptanceOracle | LibraryAcceptance:
    """Select the verifier oracle required by the locked root Component kind."""

    locked = prepared.locked_authority_snapshot.authority
    if (
        getattr(getattr(locked, "root_authoring", None), "resolved_kind", None)
        != "library"
    ):
        return FilesystemQualificationAcceptanceOracle(profile, locked.lock)
    name = root_component_name(locked.lock)
    try:
        oracle = load_library_acceptance(
            oracle_path(project.root, name),
            name,
            **({} if read_bytes is None else {"read_bytes": read_bytes}),
        )
    except ComponentAcceptanceError as exc:
        raise FilesystemQualificationError(exc.code, exc.message) from exc
    profile_cases = [
        [case.case_id, list(case.arguments), case.expected_result]
        for case in profile.cases
    ]
    oracle_cases = [
        [case.case_id, case.arguments, case.expected_result] for case in oracle.cases
    ]
    if canonical_json_bytes(oracle_cases) != canonical_json_bytes(profile_cases):
        raise FilesystemQualificationError(
            "qualification.library_oracle_profile_mismatch",
            "library qualification profile differs from verifier-owned acceptance",
        )
    return oracle


class FilesystemStandardQualificationLifecyclePort:
    """Drive a separately composed, cache-empty Standard rebuild for every run."""

    def __init__(
        self,
        *,
        project: LoadedProject,
        prepared: PreparedLockedGeneration,
        binding: ResolvedStandardProjectLifecycleDriver,
        invalidation: ComponentInvalidationDecision,
        workspaces: FilesystemQualificationWorkspaceAllocator,
        profile: LocalQualificationProfile,
        max_parallelism: int = 1,
        retain_library_products: bool = False,
        pipeline_model: str | None = None,
    ) -> None:
        self.pipeline_model = pipeline_model
        self.project = project
        self.prepared = prepared
        self.binding = binding
        self.invalidation = invalidation
        self.workspaces = workspaces
        self.max_parallelism = max_parallelism
        self.retain_library_products = retain_library_products
        self.evidence_recorder: QualificationEvidenceRecorder | None = None
        self.product_sources: dict[
            str, tuple[QualificationLifecycleExecution, LocalStandardLifecyclePorts]
        ] = {}
        self.profile = profile
        self.acceptance_oracle: LocalIndependentAcceptanceOracle | LibraryAcceptance = (
            _qualification_acceptance_oracle(project, prepared, profile)
        )
        expected = {
            item.revision.identity.uri
            for item in prepared.locked_authority_snapshot.authority.lock.nodes
        }
        regenerated = {item.uri for item in invalidation.regenerate}
        if regenerated != expected:
            raise FilesystemQualificationError(
                "qualification.cache_bypass_incomplete",
                "qualification invalidation must force every locked Component",
            )
        self.generated_roots: dict[str, tuple[tuple[ContentIdentity, Path], ...]] = {}
        self.root_generated_roots: dict[str, Path] = {}
        self.run_roots: dict[str, Path] = {}

    def require_current_authority(self) -> None:
        """Reopen provider authority before retained capture or final admission."""

        self.prepared.locked_authority_snapshot.require_unchanged()
        try:
            self.binding.require_unchanged()
        except StandardLifecycleBindingError as exc:
            raise FilesystemQualificationError(exc.code, exc.message) from exc
        if self.project.definition.lifecycle_driver != self.binding.driver:
            raise FilesystemQualificationError(
                "qualification.standard_binding_changed",
                "configured Standard lifecycle driver changed during qualification",
            )
        current = _qualification_acceptance_oracle(
            self.project, self.prepared, self.profile
        )
        if current.identity != self.acceptance_oracle.identity:
            raise FilesystemQualificationError(
                "qualification.acceptance_oracle_changed",
                "independent acceptance authority changed during qualification",
            )

    def execute(
        self, request: QualificationLifecycleRequest
    ) -> QualificationLifecycleExecution:
        if request.force_regeneration is not True:
            raise FilesystemQualificationError(
                "qualification.cache_bypass_required",
                "filesystem qualification requires forced source regeneration",
            )
        if (
            request.component_lock_identity
            != self.prepared.locked_authority_snapshot.authority.lock.identity
        ):
            raise FilesystemQualificationError(
                "qualification.component_lock_mismatch",
                "qualification request differs from the prepared Component lock",
            )
        root = self.workspaces.resolve(request.workspace.allocation_identity)
        self.run_roots[request.run_identity.uri] = root
        adapter = assemble_filesystem_standard_rebuild_adapter(
            project=self.project,
            prepared=self.prepared,
            object_root=root / "objects",
            generated_source_cache_root=root / "generated-source-cache",
            source_cache_root=root / "accepted-source-cache",
            candidate_cas_root=root / "candidate-cas",
            accepted_cas_root=root / "accepted-source-cas",
            checkpoint_root=root / "checkpoints",
            binding=self.binding,
            independent_acceptance_oracle=self.acceptance_oracle,
            pipeline_model=self.pipeline_model,
        )
        if self.evidence_recorder is not None:
            adapter.runtime.lifecycle_ports.retain_evidence_with(self.evidence_recorder)
            indexer = adapter.runtime.application.lifecycle.indexer
            retain_index = getattr(indexer, "retain_evidence_with", None)
            if not callable(retain_index):
                raise FilesystemQualificationError(
                    "qualification.index_capture_unsupported",
                    "selected source indexer cannot retain its qualification evidence",
                )
            if indexer is not adapter.runtime.lifecycle_ports:
                retain_index(self.evidence_recorder)
        try:
            rebuilt = adapter.rebuild(
                FilesystemStandardRebuildRequest(
                    self.prepared,
                    root / "component-workspaces",
                    self.invalidation,
                    update_receipt=False,
                    max_parallelism=self.max_parallelism,
                )
            )
        except FilesystemStandardRebuildError as exc:
            raise FilesystemQualificationError(exc.code, exc.message) from exc
        roots = tuple(
            sorted(
                (
                    (
                        node.source_output.candidate.tree_identity,
                        adapter.runtime.source_trees.resolve(
                            node.source_output.candidate.tree_identity
                        ),
                    )
                    for node in rebuilt.execution.lifecycle.node_results
                    if node.source_output is not None
                ),
                key=lambda item: item[0].uri,
            )
        )
        self.generated_roots[request.run_identity.uri] = roots
        root_node = next(
            node
            for node in rebuilt.execution.lifecycle.node_results
            if node.component_revision
            == self.prepared.locked_authority_snapshot.authority.lock.root_revision
        )
        if root_node.source_output is None:
            raise FilesystemQualificationError(
                "qualification.root_tree_missing",
                "accepted lifecycle omitted the root Component generated tree",
            )
        self.root_generated_roots[request.run_identity.uri] = (
            adapter.runtime.source_trees.resolve(
                root_node.source_output.candidate.tree_identity
            )
        )
        execution = QualificationLifecycleExecution(
            rebuilt.execution.lifecycle,
            rebuilt.receipt,
            rebuilt.lifecycle_request_identity,
            rebuilt.lifecycle_invocation_identity,
            self.binding.driver.identity,
            self.binding.policy.identity,
            self.binding.distribution.identity,
        )
        if self.evidence_recorder is not None:
            capture_lifecycle_records(execution.lifecycle, self.evidence_recorder)
            self.evidence_recorder.remember_json(execution.receipt.to_dict())
            snapshot = self.prepared.locked_authority_snapshot
            snapshot.require_unchanged()
            projector = adapter.runtime.node_preparation
            current_plan = rebuilt.execution.planned.execution_plan
            if current_plan.identity != execution.lifecycle.execution_plan_identity:
                raise QualificationCaptureError(
                    "qualification.capture.lifecycle-mismatch"
                )
            plans = {
                plan.component_revision: plan for plan in current_plan.generation_plans
            }
            reader = QualificationEvidenceReader(
                self.evidence_recorder.entries,
                max_bytes=self.evidence_recorder.max_bytes,
                max_records=self.evidence_recorder.max_records,
            )
            for node in execution.lifecycle.node_results:
                projection = projector.project(snapshot, plans[node.component_revision])
                verify_qualification_generated_suite(
                    reader,
                    recipe=projection.recipe,
                    component_lock_identity=snapshot.authority.lock.identity,
                    tests=node.generated_test_evidence,
                )
                capture_qualification_source_records(
                    self.evidence_recorder,
                    candidate=node.source_output.candidate,
                    cas=FileSystemCAS(root / "candidate-cas", create=False),
                )
            snapshot.require_unchanged()
        if self.retain_library_products:
            self.product_sources[request.run_identity.uri] = (
                execution,
                adapter.runtime.lifecycle_ports,
            )
        return execution


class FilesystemQualificationParityVerifier:
    """Execute every pinned baseline/generated case against the retained root tree."""

    def __init__(
        self,
        *,
        source_root: Path,
        source_snapshot_identity: ContentIdentity,
        profile: LocalQualificationProfile,
        lifecycle: FilesystemStandardQualificationLifecyclePort,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.source_root = source_root.resolve(strict=True)
        self.source_snapshot_identity = source_snapshot_identity
        self.profile = profile
        self.lifecycle = lifecycle
        self.environment = dict(os.environ if environment is None else environment)
        self.case_map = qualification_verifier_case_map(
            profile, source_snapshot_identity
        )
        self._provider_identity = self.case_map.verifier_identity
        self._case_values = {
            canonical_identity(case.to_dict()): case for case in self.profile.cases
        }

    @property
    def provider_identity(self) -> ContentIdentity:
        return self._provider_identity

    def verify(
        self,
        *,
        run_identity: ContentIdentity,
        source_snapshot_identity: ContentIdentity,
        generated_tree_identities: tuple[ContentIdentity, ...],
        case_map: QualificationVerifierCaseMap,
    ) -> QualificationParityEvidence:
        if (
            source_snapshot_identity != self.source_snapshot_identity
            or (source_inventory := inventory_source(self.source_root)).identity
            != source_snapshot_identity.uri
            or case_map != self.case_map
        ):
            raise FilesystemQualificationError(
                "qualification.parity_authority_mismatch",
                "parity inputs differ from the pinned source or verifier case map",
            )
        retained = self.lifecycle.generated_roots.get(run_identity.uri)
        if (
            retained is None
            or tuple(item[0] for item in retained) != generated_tree_identities
        ):
            raise FilesystemQualificationError(
                "qualification.generated_tree_custody_missing",
                "parity lacks custody of the exact generated lifecycle trees",
            )
        generated_root = self.lifecycle.root_generated_roots.get(run_identity.uri)
        if generated_root is None:
            raise FilesystemQualificationError(
                "qualification.root_tree_missing",
                "generated root Component tree has no retained custody",
            )
        native_root = generated_root.joinpath(*Path(self.profile.generated_root).parts)
        try:
            native_root = require_safe_directory(native_root)
        except UnsafeFilesystemPathError as exc:
            raise FilesystemQualificationError(
                "qualification.generated_native_root_missing",
                "generated root Component lacks the profile's safe native source root",
            ) from exc
        build_root = self.workspaces_root(run_identity)
        recorder = getattr(self.lifecycle, "evidence_recorder", None)
        if recorder is not None:
            if (
                recorder.remember_json(canonical_value(source_inventory))
                != source_snapshot_identity
            ):
                raise QualificationCaptureError(
                    "qualification.capture.baseline-inventory-mismatch"
                )
            recorder.remember_json(self.profile.to_dict())
            recorder.remember_json(case_map.to_dict())
            recorder.remember_json(
                qualification_verifier_record(
                    self.profile, self.source_snapshot_identity
                )
            )
            for case in self.profile.cases:
                recorder.remember_json(case.to_dict())
        cases: list[QualificationParityCaseEvidence] = []
        for binding in case_map.cases:
            argument = _json_argument(
                self._case_values[binding.case_identity].arguments
            )
            source_value, source_observation, source_valid = _observe(
                (*self.profile.source_command, argument),
                cwd=self.source_root,
                environment=self.environment,
                timeout_seconds=self.profile.timeout_seconds,
                maximum_output_bytes=self.profile.maximum_output_bytes,
                recorder=recorder,
            )
            generated_value, generated_observation, generated_valid = _observe(
                (
                    *_expand(
                        self.profile.generated_command,
                        workspace=generated_root,
                        build_root=build_root,
                    ),
                    argument,
                ),
                cwd=native_root,
                environment=self.environment,
                timeout_seconds=self.profile.timeout_seconds,
                maximum_output_bytes=self.profile.maximum_output_bytes,
                identity_command=(*self.profile.generated_command, argument),
                recorder=recorder,
            )
            cases.append(
                QualificationParityCaseEvidence(
                    binding.case_id,
                    binding.case_identity,
                    source_observation,
                    generated_observation,
                    source_valid
                    and generated_valid
                    and canonical_digest(source_value)
                    == canonical_digest(generated_value),
                )
            )
        if inventory_source(self.source_root).identity != source_snapshot_identity.uri:
            raise FilesystemQualificationError(
                "qualification.source_drift",
                "source baseline changed during independent parity execution",
            )
        return QualificationParityEvidence(
            run_identity,
            source_snapshot_identity,
            generated_tree_identities,
            self.provider_identity,
            case_map.identity,
            tuple(cases),
        )

    def workspaces_root(self, run_identity: ContentIdentity) -> Path:
        return self.lifecycle.run_roots[run_identity.uri] / "objects"


class FilesystemStandardQualificationAdapter:
    """Public application adapter for two-or-more clean Standard lifecycle runs."""

    def __init__(
        self,
        *,
        project: LoadedProject,
        prepared: PreparedLockedGeneration,
        binding: ResolvedStandardProjectLifecycleDriver,
        invalidation: ComponentInvalidationDecision,
        source_root: Path,
        source_snapshot_identity: ContentIdentity,
        profile: LocalQualificationProfile,
        scratch_root: Path,
        max_parallelism: int = 1,
        retain_library_products: bool = False,
        max_capture_bytes: int = 256 * 1024 * 1024,
        pipeline_model: str | None = None,
    ) -> None:
        if type(retain_library_products) is not bool or (
            type(max_capture_bytes) is not int or max_capture_bytes <= 0
        ):
            raise FilesystemQualificationError(
                "qualification.capture_configuration_invalid",
                "product retention requires a boolean selector and positive byte limit",
            )
        if retain_library_products and (
            prepared.locked_authority_snapshot.authority.root_authoring.resolved_kind
            != "library"
        ):
            raise FilesystemQualificationError(
                "qualification.capture_library_required",
                "product retention requires a library root Component",
            )
        self.max_capture_bytes = max_capture_bytes
        self.library_captures: tuple[QualificationRunCapture, ...] = ()
        self.evidence_blobs: tuple[tuple[ContentIdentity, bytes], ...] = ()
        workspaces = FilesystemQualificationWorkspaceAllocator(scratch_root)
        lifecycle = FilesystemStandardQualificationLifecyclePort(
            project=project,
            prepared=prepared,
            binding=binding,
            invalidation=invalidation,
            workspaces=workspaces,
            profile=profile,
            max_parallelism=max_parallelism,
            retain_library_products=retain_library_products,
            pipeline_model=pipeline_model,
        )
        self.workspaces = workspaces
        self.lifecycle = lifecycle
        self.parity = FilesystemQualificationParityVerifier(
            source_root=source_root,
            source_snapshot_identity=source_snapshot_identity,
            profile=profile,
            lifecycle=lifecycle,
        )
        self.runner = QualificationLifecycleRunner(
            workspaces=workspaces,
            lifecycle=lifecycle,
            parity_verifier=self.parity,
        )

    def qualify(self, plan: QualificationLifecyclePlan) -> QualificationLifecycleResult:
        self.library_captures = ()
        self.evidence_blobs = ()
        self.lifecycle.product_sources.clear()
        if plan.case_map != self.parity.case_map:
            raise FilesystemQualificationError(
                "qualification.case_map_mismatch",
                "qualification plan does not use the filesystem verifier's exact cases",
            )
        recorder = (
            QualificationEvidenceRecorder(
                max_bytes=self.max_capture_bytes, max_records=100_000
            )
            if self.lifecycle.retain_library_products
            else None
        )
        self.lifecycle.evidence_recorder = recorder
        try:
            result = self.runner.run(plan)
            self.lifecycle.require_current_authority()
            if self.lifecycle.retain_library_products:
                captures = []
                assert recorder is not None
                for record in (
                    result,
                    result.case_map,
                    *result.runs,
                    *result.parity_evidence,
                    *(
                        case
                        for parity in result.parity_evidence
                        for case in parity.cases
                    ),
                ):
                    recorder.remember_json(record.to_dict())
                pending = []
                for run in result.runs:
                    execution, ports = self.lifecycle.product_sources[
                        run.run_identity.uri
                    ]
                    integration = execution.lifecycle.root_integration
                    if (
                        integration is None
                        or execution.lifecycle.identity != run.lifecycle_result_identity
                        or execution.receipt.identity != run.project_receipt_identity
                    ):
                        raise QualificationCaptureError(
                            "qualification.capture.lifecycle-mismatch"
                        )
                    ports.project_package_custody(
                        integration.package_plan, integration.package_result
                    )
                    products = tuple(
                        sorted(
                            (
                                LibraryArtifactProduct(
                                    export,
                                    ports.contracts[
                                        export.component_revision.uri
                                    ].library_import_surface,
                                    getattr(
                                        ports.contracts[export.component_revision.uri],
                                        "native_layout",
                                        None,
                                    ),
                                )
                                for manifest in integration.artifact_graph.manifests
                                for export in manifest.exports
                                if export.role == "library"
                            ),
                            key=lambda item: item.artifact_export.identity.uri,
                        )
                    )
                    export_set = RetainedLibraryExportSet(
                        integration.artifact_graph,
                        integration.link_plan.identity,
                        products,
                    )
                    acceptances = tuple(
                        sorted(
                            (
                                node.acceptance_evidence
                                for node in execution.lifecycle.node_results
                            ),
                            key=lambda item: item.identity.uri,
                        )
                    )
                    for record in (
                        export_set,
                        *products,
                        *(product.import_surface for product in products),
                        *acceptances,
                    ):
                        recorder.remember_json(record.to_dict())
                    pending.append((run, export_set, acceptances, ports))
                remaining = self.max_capture_bytes - recorder.retained_bytes
                for run, export_set, acceptances, ports in pending:
                    captured = capture_qualification_run(
                        run,
                        export_set,
                        acceptances,
                        read_blob=ports.read_artifact_blob,
                        max_bytes=remaining,
                    )
                    remaining -= sum(ref.size for ref, _ in captured.blobs)
                    for _, payload in captured.blobs:
                        recorder.remember_bytes(payload)
                    captures.append(captured)
                self.lifecycle.require_current_authority()
                self.library_captures = tuple(captures)
                self.evidence_blobs = recorder.entries
            return result
        finally:
            # Captures contain immutable bytes, not runtimes or scratch paths.
            # A failed later run cannot leave an earlier run exposed as qualified.
            self.lifecycle.product_sources.clear()
            self.lifecycle.evidence_recorder = None


__all__ = [
    "FilesystemQualificationError",
    "FilesystemQualificationAcceptanceOracle",
    "FilesystemQualificationParityVerifier",
    "FilesystemQualificationWorkspaceAllocator",
    "FilesystemStandardQualificationAdapter",
    "FilesystemStandardQualificationLifecyclePort",
]
