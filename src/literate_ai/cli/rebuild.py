"""Fail-closed project driver for a complete specification-led host rebuild."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from literate_ai.adapters.component_lock_application import (
    ProjectComponentLockSetError,
    current_project_component_lock_identities,
    project_component_roots,
)
from literate_ai.adapters.generation_preparation import (
    FilesystemLockedGenerationApplicationAdapter,
    FilesystemLockedGenerationPreparationAdapter,
    GenerationPreparationError,
)
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.adapters.lifecycle_lock import (
    ProjectLifecycleLockError,
    project_lifecycle_lock,
)
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.models import CodingCliError
from literate_ai.adapters.native_sdk_acquisition import NativeSdkProjectAcquisition
from literate_ai.adapters.project_lifecycle_driver import (
    ProjectLifecycleDriverAdapterError,
    bind_external_project_lifecycle_driver,
)
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
)
from literate_ai.adapters.project_validation import (
    validated_project_authority_identity as adapter_project_authority_identity,
)
from literate_ai.adapters.retained_source import (
    RetainedSourceError,
    RetainedSourceInput,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    ResolvedStandardProjectLifecycleDriver,
    StandardLifecycleBindingError,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.adapters.standard_rebuild import (
    FilesystemStandardRebuildError,
    FilesystemStandardRebuildRequest,
    assemble_filesystem_standard_rebuild_adapter,
)
from literate_ai.application import GenerationPreparationRequest
from literate_ai.application.standard_test_receipts import (
    StandardTestReceiptProjectionError,
    combine_standard_project_test_receipts,
)
from literate_ai.cache_directories import (
    CacheDirectories,
    CacheDirectoryError,
    select_cache_directories,
)
from literate_ai.contracts import (
    PROJECT_LIFECYCLE_EXTENSION_PHASES,
    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
    ComponentChangeSurface,
    ComponentInvalidationDecision,
    ContentIdentity,
    ProjectLifecycleDriver,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptProvisional,
    SourceIntelligenceStage,
    StandardProjectLifecycleDriver,
    StandardSourceSelector,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    canonical_identity,
    canonical_json_bytes,
    flavor_selector,
    rebuild_project_authority_identity,
)
from literate_ai.project_source_index import (
    ProjectSourceIntelligenceError,
    require_lifecycle_project_index,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    LoadedProject,
    ProjectError,
    discover_project,
    load_project,
)
from literate_ai.test_receipts import (
    load_project_test_receipt_candidate,
    load_project_test_receipt_provisional,
    update_project_test_receipt_finalized_value,
    validate_project_test_receipt_provisional,
)

from .errors import CLI_ERROR_MESSAGE_CHARS, CliFailure
from .rebuild_cache import (
    RebuildSourceCacheProtocolError,
    parse_rebuild_source_cache_inputs,
    prepare_rebuild_source_cache_planning_protocol,
    prepare_rebuild_source_cache_protocol,
    publish_rebuild_source_cache_offer,
    validate_rebuild_source_cache_decision,
    validate_rebuild_source_cache_lifecycle,
)

PROJECT_REBUILD_RESULT_SCHEMA = "literate-ai/project-rebuild@1"
PROJECT_REBUILD_REQUEST_SCHEMA = "literate-ai/project-rebuild-request@3"
PROJECT_REBUILD_PLANNING_REQUEST_SCHEMA = (
    "literate-ai/project-rebuild-planning-request@2"
)
PROJECT_REBUILD_DAG_SCHEMA = "literate-ai/project-rebuild-dag@1"
_PROVISIONAL_RECEIPT_FILENAME = "project-test-receipt.provisional.json"


def _standard_rebuild_dag(
    prepared: Any, execution_plan: Any, lifecycle: Any
) -> dict[str, object]:
    """Project the exact build-phase DAG into compact presentation evidence."""

    lock = prepared.locked_authority_snapshot.authority.lock
    build_action = next(
        item for item in execution_plan.action_plans if item.phase.value == "build"
    )
    coordinate_by_revision = {
        item.revision.identity.uri: item.revision.coordinate.uri for item in lock.nodes
    }
    disposition_by_revision = {
        item.component_revision.uri: item.disposition.value
        for item in lifecycle.node_results
    }
    return {
        "schema": PROJECT_REBUILD_DAG_SCHEMA,
        "root_revision": lock.root_revision.uri,
        "layers": [
            {
                "index": layer.index,
                "components": [
                    {
                        "coordinate": coordinate_by_revision[revision.uri],
                        "revision": revision.uri,
                        "source": disposition_by_revision[revision.uri],
                    }
                    for revision in layer.component_revisions
                ],
            }
            for layer in build_action.layers
        ],
        "edges": [
            {
                "provider": coordinate_by_revision[edge.provider_revision.uri],
                "consumer": coordinate_by_revision[edge.consumer_revision.uri],
                "kind": edge.kind.value,
                "capability": edge.capability,
            }
            for edge in build_action.dependency_edges
        ],
    }


def _locked_standard_component_roots(project: LoadedProject) -> tuple[Path, ...]:
    """Return committed lock-set members in stable catalog order."""

    selected: list[Path] = []
    for component in project_component_roots(project):
        lock_path = component / "component.lock.json"
        try:
            lock_path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CliFailure(
                "rebuild.standard_lock_set_unavailable",
                "project Component lock set could not be inspected",
            ) from exc
        if lock_path.is_symlink() or not lock_path.is_file():
            raise CliFailure(
                "rebuild.standard_lock_unsafe",
                "committed Component locks cannot be redirected or irregular",
            )
        selected.append(component)
    return tuple(
        sorted(
            selected,
            key=lambda item: item.relative_to(project.root).as_posix(),
        )
    )


def _sum_test_summary(
    summaries: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    totals = {"failed": 0, "passed": 0, "skipped": 0, "total": 0}
    for summary in summaries:
        for key in totals:
            totals[key] += int(summary.get(key, 0) or 0)
    return totals


def _aggregate_standard_rebuild_results(
    results: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Present one project-root rebuild from exact per-Component results."""

    if not results:
        raise CliFailure(
            "rebuild.standard_lock_set_empty",
            "Standard project-root rebuild produced no Component results",
        )
    lock_identities: list[str] = []
    seen_locks: set[str] = set()
    source_generation: dict[str, object] = {}
    layers: list[dict[str, object]] = []
    edges: list[object] = []
    cache = {"build_seconds": 0.0, "hit_seconds": 0.0, "hits": 0, "misses": 0}
    layer_index = 0
    for result in results:
        for uri in result.get("component_lock_identities") or ():
            if not isinstance(uri, str) or uri in seen_locks:
                continue
            seen_locks.add(uri)
            lock_identities.append(uri)
        source_generation.update(
            {
                key: value
                for key, value in dict(result.get("source_generation") or {}).items()
            }
        )
        dag = result.get("dag") if isinstance(result.get("dag"), dict) else {}
        for layer in dag.get("layers") or ():
            if isinstance(layer, dict):
                layers.append({**layer, "index": layer_index})
                layer_index += 1
        edges.extend(dag.get("edges") or ())
        report = (
            result.get("build_cache")
            if isinstance(result.get("build_cache"), dict)
            else {}
        )
        cache["build_seconds"] += float(report.get("build_seconds") or 0)
        cache["hit_seconds"] += float(report.get("hit_seconds") or 0)
        cache["hits"] += int(report.get("hits") or 0)
        cache["misses"] += int(report.get("misses") or 0)
    aggregated = {
        key: value
        for key, value in results[0].items()
        if all(key in result and result[key] == value for result in results[1:])
    }
    aggregated["specification"] = "."
    aggregated["component_lock_identities"] = lock_identities
    aggregated["test_summary"] = _sum_test_summary(
        tuple(
            dict(result.get("test_summary") or {})
            for result in results
            if isinstance(result.get("test_summary"), dict)
        )
    )
    aggregated["source_generation"] = source_generation
    aggregated["build_cache"] = cache
    aggregated["dag"] = {
        "schema": PROJECT_REBUILD_DAG_SCHEMA,
        "layers": layers,
        "edges": edges,
    }
    aggregated["components"] = [dict(result) for result in results]
    return aggregated


def validated_project_authority_identity(
    selected: Path, *, synchronize_source_intelligence: bool = True
) -> ContentIdentity:
    """Translate adapter validation failures into the CLI error envelope."""

    try:
        return adapter_project_authority_identity(
            selected,
            synchronize_source_intelligence=synchronize_source_intelligence,
        )
    except ProjectValidationError as exc:
        raise CliFailure(exc.code, exc.message) from exc


_FULL_REBUILD_EVIDENCE = frozenset(STANDARD_FULL_REBUILD_EVIDENCE_KINDS)


def _resolve_standard_binding(
    driver: StandardProjectLifecycleDriver,
) -> ResolvedStandardProjectLifecycleDriver:
    """Reject unknown or drifting Standard authority before allocating host paths."""

    try:
        resolved = resolve_standard_project_lifecycle_driver(driver)
        resolved.require_unchanged()
        return resolved
    except StandardLifecycleBindingError as exc:
        if exc.code == "standard_binding.distribution_mismatch":
            code = "rebuild.standard_distribution_mismatch"
        elif exc.code == "standard_binding.policy_mismatch":
            code = "rebuild.standard_policy_mismatch"
        elif exc.code == "standard_binding.changed":
            code = "rebuild.standard_binding_changed"
        elif exc.code.startswith("standard_binding.policy_"):
            code = "rebuild.standard_policy_unavailable"
        else:
            code = "rebuild.standard_distribution_unavailable"
        raise CliFailure(code, exc.message) from exc


def _write_standard_candidate(path: Path, finalized: object) -> None:
    """Write the finalized in-process Standard receipt to one new external path."""

    content = canonical_json_bytes(finalized.to_dict()) + b"\n"
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise CliFailure(
            "rebuild.candidate_write_failed",
            "Standard candidate receipt could not be written atomically",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _standard_root_product_result(
    lifecycle_ports: Any,
    root_build_plan: Any,
    root_exports: tuple[Any, ...],
    root_export: Any,
) -> tuple[Any | None, dict[str, object] | None]:
    """Project either executable invocation or importable-library result authority."""

    command_contract = lifecycle_ports.contracts[root_build_plan.component_revision.uri]
    if not command_contract.is_library:
        return (
            lifecycle_ports.execution_command(root_build_plan, root_exports),
            None,
        )
    library_surface = command_contract.library_import_surface
    if library_surface is None:
        raise CliFailure(
            "standard_rebuild.library_import_surface_unavailable",
            "accepted library command authority did not retain its typed "
            "import surface",
        )
    from literate_ai.contracts.library_products import LibraryArtifactProduct

    try:
        product = LibraryArtifactProduct(
            root_export,
            library_surface,
            getattr(command_contract, "native_layout", None),
        )
        if (
            product.artifact_export.component_revision
            != root_build_plan.component_revision
        ):
            raise ValueError("library product belongs to another Component revision")
    except ValueError as exc:
        raise CliFailure(
            "standard_rebuild.library_artifact_invalid",
            "accepted library result must retain typed package and import authority",
        ) from exc
    return None, product.to_dict()


def _standard_rebuild_from_args(
    project: LoadedProject,
    binding: ResolvedStandardProjectLifecycleDriver,
    args: Any,
    *,
    cache_directories: CacheDirectories | None = None,
    observer: Callable[[Any, Any, Any, Any], None] | None = None,
) -> dict[str, object]:
    """Route the ordinary command through the public in-process Standard adapter."""

    if args.source_cache_entry or args.source_cache_root:
        raise CliFailure(
            "rebuild.standard_cache_override_unsupported",
            "the Standard driver uses BUILD_DIR-backed cache custody; per-command "
            "--source-cache-entry and --source-cache-root overrides are only "
            "supported for an external lifecycle driver. For deterministic "
            "Standard reuse of already-accepted source, use "
            "--from-accepted-source instead",
        )
    specification, specification_label = _specification(project, args.specification)
    if specification != project.root:
        return _standard_rebuild_one_component(
            project,
            binding,
            args,
            specification,
            specification_label,
            cache_directories=cache_directories,
            observer=observer,
            update_receipt=bool(getattr(args, "update_receipt", False)),
            write_candidate=True,
            hold_lock=True,
        )
    try:
        components = _locked_standard_component_roots(project)
    except ProjectComponentLockSetError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if not components:
        raise CliFailure(
            "rebuild.standard_lock_set_empty",
            "Standard project-root rebuild requires at least one committed "
            "Component lock; lock a Component or pass its path",
        )
    labels = tuple(
        component.relative_to(project.root).as_posix() for component in components
    )
    operation = (
        "update-receipt" if getattr(args, "update_receipt", False) else "rebuild"
    )
    configured_runtime = getattr(args, "runtime_root", None)
    shared_runtime = (
        _external_runtime_root(project, configured_runtime)
        if configured_runtime is not None and len(components) > 1
        else None
    )
    results: list[dict[str, object]] = []
    candidates: list[ProjectTestReceiptFinalizedCandidate] = []
    snapshots = []

    def collect(rebuilt, adapter, prepared, directories):
        candidates.append(rebuilt.finalized_receipt)
        snapshots.append(prepared.locked_authority_snapshot)
        if observer is not None:
            observer(rebuilt, adapter, prepared, directories)

    multiple = len(components) > 1
    candidate_path = (
        _external_candidate(project, args.candidate_receipt)
        if multiple and getattr(args, "candidate_receipt", None) is not None
        else None
    )
    try:
        with project_lifecycle_lock(project.root, operation=operation):
            if multiple:
                if load_project(project.root).definition != project.definition:
                    raise CliFailure(
                        "rebuild.standard_lock_set_changed",
                        "project configuration changed before execution",
                    )
                validated = validated_project_authority_identity(project.root)
                locks = current_project_component_lock_identities(project)
                # Validate the finite, canonical set before running any member.
                project_revision = rebuild_project_authority_identity(validated, locks)
                if _locked_standard_component_roots(project) != components:
                    raise CliFailure(
                        "rebuild.standard_lock_set_changed",
                        "Component lock set changed before execution",
                    )
            for component, label in zip(components, labels, strict=True):
                override = (
                    None
                    if shared_runtime is None
                    else shared_runtime.joinpath(*Path(label).parts)
                )
                results.append(
                    _standard_rebuild_one_component(
                        project,
                        binding,
                        args,
                        component,
                        label,
                        cache_directories=cache_directories,
                        observer=collect if multiple else observer,
                        update_receipt=bool(getattr(args, "update_receipt", False))
                        and not multiple,
                        write_candidate=not multiple,
                        hold_lock=False,
                        runtime_root_override=override,
                    )
                )
            aggregated = _aggregate_standard_rebuild_results(results)
            if multiple:
                binding.require_unchanged()
                for snapshot in snapshots:
                    snapshot.require_unchanged()
                if (
                    _locked_standard_component_roots(project) != components
                    or current_project_component_lock_identities(project) != locks
                    or validated_project_authority_identity(project.root) != validated
                ):
                    raise CliFailure(
                        "rebuild.standard_lock_set_changed",
                        "project authority or Component lock set changed "
                        "during execution",
                    )
                finalized = combine_standard_project_test_receipts(
                    tuple(candidates),
                    project_id=project.definition.project_id,
                    validated_project_identity=validated,
                    component_lock_identities=locks,
                    lifecycle_policy=binding.policy,
                    receipt_policy=project.definition.test_receipt_policy,
                )
                receipt_update = (
                    update_project_test_receipt_finalized_value(
                        project, finalized, project_revision_identity=validated
                    )
                    if getattr(args, "update_receipt", False)
                    else None
                )
                if candidate_path is not None:
                    _write_standard_candidate(candidate_path, finalized)
                aggregated.update(
                    project_revision_identity=project_revision.uri,
                    validated_project_authority_identity=validated.uri,
                    component_lock_identities=[
                        item.uri for item in finalized.component_lock_identities
                    ],
                    lifecycle_request_identity=finalized.lifecycle_request_identity.uri,
                    lifecycle_invocation_identity=finalized.lifecycle_command_identity.uri,
                    lifecycle_result_identity=finalized.receipt.result_identity.uri,
                    receipt_identity=finalized.receipt_identity.uri,
                    source_cache_lifecycle_identity=finalized.source_cache_lifecycle_identity.uri,
                    finalized_candidate_identity=finalized.identity.uri,
                    receipt_committed=receipt_update is not None,
                    receipt_update=receipt_update,
                    candidate_receipt=None
                    if candidate_path is None
                    else str(candidate_path),
                    test_summary=finalized.receipt.summary.to_dict(),
                )
                # Singular products belong to the per-Component records only.
                for key in (
                    "component_lock_identity",
                    "artifact",
                    "execution_command",
                    "library_artifact",
                    "execution_entrypoints",
                ):
                    aggregated.pop(key, None)
    except (
        ProjectLifecycleLockError,
        ProjectComponentLockSetError,
        ProjectError,
        StandardTestReceiptProjectionError,
        GenerationPreparationError,
        LockedGenerationAuthorityReaderError,
        StandardLifecycleBindingError,
    ) as exc:
        raise CliFailure(exc.code, exc.message) from exc
    return aggregated


def _standard_rebuild_one_component(
    project: LoadedProject,
    binding: ResolvedStandardProjectLifecycleDriver,
    args: Any,
    specification: Path,
    specification_label: str,
    *,
    cache_directories: CacheDirectories | None = None,
    observer: Callable[[Any, Any, Any, Any], None] | None = None,
    update_receipt: bool,
    write_candidate: bool,
    hold_lock: bool,
    runtime_root_override: Path | None = None,
) -> dict[str, object]:
    """Rebuild one locked Component through the public Standard adapter."""

    explicit_selectors = tuple(
        flavor_selector(value, f"rebuild Flavor selector[{index}]")
        for index, value in enumerate(args.flavor)
    )
    selectors = project.flavor_selectors_for(specification, explicit_selectors)
    try:
        directories = cache_directories or _selected_cache_directories(
            project.root, args
        )
        if directories.project_root != project.root.resolve(strict=True):
            raise CacheDirectoryError(
                "explicit cache directory custody belongs to a different project"
            )
        from literate_ai.projects import discover_project
        from literate_ai.security import (
            AuthorizationError,
            AuthorizationRevocationSet,
            SecurityPolicy,
        )

        revocations = AuthorizationRevocationSet()
        reviewed_project_bytes = None

        def current_sdk_revocations():
            nonlocal reviewed_project_bytes
            binding.require_unchanged()
            path = project.root / "literate.project.json"
            if reviewed_project_bytes is None:
                content = path.read_bytes()
                current = discover_project(project.root)
                if current is None or current.definition != project.definition:
                    raise AuthorizationError("native_sdk.project_authority_changed")
                validated_project_authority_identity(project.root)
                reviewed_project_bytes = content
            if path.read_bytes() != reviewed_project_bytes:
                raise AuthorizationError("native_sdk.project_authority_changed")
            return revocations

        acquisition = NativeSdkProjectAcquisition(
            cache_root=directories.build_dir / "native-sdk-cache",
            source_intelligence_policy=project.definition.source_intelligence,
            security_policy=SecurityPolicy(
                canonical_identity(
                    {
                        "policy": "standard-native-sdk-build@1",
                        "standard_policy": binding.policy.identity.uri,
                    }
                ).uri
            ),
            revocations=current_sdk_revocations,
            environment=os.environ,
            actor="standard-rebuild",
            reason="Build the locked SDK dependencies of the acknowledged project",
            host_build_acknowledged=args.allow_host_execution,
        )

        def acquire_sdk_inputs(snapshot):
            if any(
                node.revision.repository_sources
                for node in snapshot.authority.lock.nodes
            ):
                with project_lifecycle_lock(project.root, operation="sdk-acquisition"):
                    return acquisition(snapshot)
            return acquisition(snapshot)

        prepared = FilesystemLockedGenerationApplicationAdapter(
            FilesystemLockedGenerationPreparationAdapter(
                native_sdk_builder=acquire_sdk_inputs
            )
        ).prepare(
            GenerationPreparationRequest(
                component_root=specification,
                target_name=getattr(args, "target", "host"),
                flavor_selectors=selectors,
                flavor_roots=project.roots("flavor"),
            )
        )
    except (
        GenerationPreparationError,
        CacheDirectoryError,
        ProjectLifecycleLockError,
    ) as exc:
        raise CliFailure(
            getattr(exc, "code", "rebuild.standard_preparation_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc
    selected_worker = getattr(args, "execution_worker", None)
    retained_source = None
    if getattr(args, "retained_source", None):
        if (
            getattr(args, "from_accepted_source", False)
            or args.force_regeneration
            or selected_worker is not None
        ):
            raise CliFailure(
                "retained_source.conflicting_mode",
                "Retained-source qualification requires local execution without "
                "regeneration or accepted-only mode",
            )
        try:
            retained_source = RetainedSourceInput.capture(
                Path(args.retained_source),
                component_lock_identity=(
                    prepared.locked_authority_snapshot.authority.lock.identity
                ),
                project_authority_identity=adapter_project_authority_identity(
                    project.root
                ),
                target=getattr(args, "target", "host"),
            )
            if getattr(args, "retained_source_plan", False):
                return {
                    "schema": "literate-ai/retained-source-review@1",
                    "input": retained_source.to_dict(),
                    "authorization_identity": retained_source.identity.uri,
                    "admitted": False,
                }
            retained_source.require_authorization(
                getattr(args, "authorize_retained_source", None)
            )
        except (RetainedSourceError, OSError) as exc:
            raise CliFailure(
                getattr(exc, "code", "retained_source.unavailable"), str(exc)
            ) from exc
    if selected_worker is not None:
        from .execution_workers import validate_worker_platform_flavors

        validate_worker_platform_flavors(selected_worker, prepared.selected_flavors)
    if runtime_root_override is not None:
        owns_runtime = False
        runtime_root = runtime_root_override
        runtime_root.mkdir(parents=True, exist_ok=True)
        if any(runtime_root.iterdir()):
            raise CliFailure(
                "rebuild.runtime_root_not_empty",
                "runtime root must be new or an empty directory",
            )
        runtime_root = runtime_root.resolve(strict=True)
    else:
        configured_runtime = getattr(args, "runtime_root", None)
        owns_runtime = configured_runtime is None
        runtime_root = (
            Path(tempfile.mkdtemp(prefix="litai-standard-rebuild-")).resolve(
                strict=True
            )
            if owns_runtime
            else _external_runtime_root(project, configured_runtime)
        )
    candidate = (
        None
        if not write_candidate or getattr(args, "candidate_receipt", None) is None
        else _external_candidate(project, args.candidate_receipt)
    )
    revisions = tuple(
        item.revision.identity
        for item in prepared.locked_authority_snapshot.authority.lock.nodes
    )
    invalidation = ComponentInvalidationDecision(
        "litai-standard-rebuild",
        prepared.locked_authority_snapshot.authority.lock.root_revision,
        (
            ComponentChangeSurface.LOCAL_AUTHORITY
            if args.force_regeneration
            else ComponentChangeSurface.SOURCE_REPLACEMENT
        ),
        revisions if args.force_regeneration else (),
        revisions,
        revisions,
    )
    operation = "update-receipt" if update_receipt else "rebuild"
    lock_context = (
        project_lifecycle_lock(project.root, operation=operation)
        if hold_lock
        else nullcontext()
    )
    try:
        with lock_context:
            try:
                adapter = assemble_filesystem_standard_rebuild_adapter(
                    project=project,
                    prepared=prepared,
                    object_root=directories.obj_dir / "standard-artifacts",
                    generated_source_cache_root=(
                        directories.build_dir / "generated-source-cache"
                    ),
                    source_cache_root=directories.build_dir / "accepted-source-cache",
                    candidate_cas_root=directories.build_dir / "candidate-cas",
                    accepted_cas_root=directories.build_dir / "accepted-source-cas",
                    checkpoint_root=directories.build_dir / "standard-checkpoints",
                    binding=binding,
                    independent_acceptance_oracle=_component_acceptance_oracle(
                        project.root,
                        specification_label,
                        prepared.locked_authority_snapshot.authority.lock,
                    ),
                    pipeline_model=getattr(args, "model", None),
                    accepted_source_provider_id=getattr(
                        args, "accepted_source_provider_id", None
                    ),
                    accepted_source_provider_identity=getattr(
                        args, "accepted_source_provider_identity", None
                    ),
                    **(
                        {
                            "retained_source": retained_source,
                            "retained_source_authorization": (
                                args.authorize_retained_source
                            ),
                        }
                        if retained_source is not None
                        else {}
                    ),
                )
                rebuilt = adapter.rebuild(
                    FilesystemStandardRebuildRequest(
                        prepared,
                        runtime_root / "sources",
                        invalidation,
                        update_receipt=update_receipt,
                        max_parallelism=getattr(args, "jobs", 1),
                        accepted_source_only=getattr(
                            args, "from_accepted_source", False
                        ),
                        source_selectors=(
                            StandardSourceSelectorSet(
                                StandardSourceSelectorScope.TARGET_SPECIFIC,
                                (
                                    StandardSourceSelector(
                                        "target.profile", args.target
                                    ),
                                ),
                            )
                            if getattr(args, "from_accepted_source", False)
                            else None
                        ),
                        directory_custody_identity=directories.identity,
                    )
                )
                if observer is not None:
                    observer(rebuilt, adapter, prepared, directories)
                root_result = next(
                    item
                    for item in rebuilt.execution.lifecycle.node_results
                    if item.component_revision
                    == prepared.locked_authority_snapshot.authority.lock.root_revision
                )
                root_export = root_result.exports[0]
                artifact = adapter.runtime.lifecycle_ports.artifact_path(root_export)
                project_build_plan = rebuilt.execution.lifecycle.project_build_plan
                if project_build_plan is None:
                    raise CliFailure(
                        "standard_rebuild.execution_command_unavailable",
                        "accepted Standard lifecycle did not retain its typed build "
                        "plan",
                    )
                root_build_plan = next(
                    item
                    for item in project_build_plan.components
                    if item.component_revision
                    == prepared.locked_authority_snapshot.authority.lock.root_revision
                )
                root_node = next(
                    item
                    for item in prepared.locked_authority_snapshot.authority.lock.nodes
                    if item.revision.identity
                    == prepared.locked_authority_snapshot.authority.lock.root_revision
                )
                authored_entrypoints = tuple(root_node.revision.definition.entrypoints)
                command_contract = adapter.runtime.lifecycle_ports.contracts[
                    root_build_plan.component_revision.uri
                ]
                execution_command, library_artifact = _standard_root_product_result(
                    adapter.runtime.lifecycle_ports,
                    root_build_plan,
                    root_result.exports,
                    root_export,
                )
                execution_entrypoints: list[dict[str, object]] = []
                if len(authored_entrypoints) > 1:
                    locked_entrypoints = command_contract.entrypoint_command_contracts()
                    if len(locked_entrypoints) != len(authored_entrypoints):
                        raise CliFailure(
                            "standard_rebuild.entrypoint_contract_mismatch",
                            "authored and locked entrypoint command cardinality "
                            "differs",
                        )
                    for authored, locked in zip(
                        authored_entrypoints, locked_entrypoints, strict=True
                    ):
                        if authored.resolved_deployment_unit != locked.deployment_unit:
                            raise CliFailure(
                                "standard_rebuild.entrypoint_contract_mismatch",
                                "authored and locked entrypoint deployment units "
                                "differ",
                            )
                        command = adapter.runtime.lifecycle_ports.execution_command(
                            root_build_plan,
                            root_result.exports,
                            entrypoint_identity=locked.entrypoint_identity,
                        )
                        execution_entrypoints.append(
                            {
                                "schema": "literate-ai/artifact-entrypoint-command@1",
                                "name": authored.name,
                                "kind": authored.kind,
                                "deployment_unit": authored.resolved_deployment_unit,
                                "argv": list(command.argv),
                                "environment": {
                                    key: value for key, value in command.environment
                                },
                            }
                        )
                    # A multi-entrypoint build must retain the shared custody directory;
                    # copying only entrypoints[0]'s exported file loses sibling outputs.
                    if execution_command is None:
                        raise CliFailure(
                            "standard_rebuild.entrypoint_contract_mismatch",
                            "an importable library cannot declare executable "
                            "entrypoints",
                        )
                    artifact = execution_command.cwd
                if candidate is not None:
                    _write_standard_candidate(candidate, rebuilt.finalized_receipt)
                result: dict[str, object] = {
                    "schema": PROJECT_REBUILD_RESULT_SCHEMA,
                    "passed": True,
                    "driver": "standard",
                    "project_id": project.definition.project_id,
                    "project_revision_identity": rebuilt.project_revision_identity.uri,
                    "validated_project_authority_identity": (
                        rebuilt.project_revision_identity.uri
                    ),
                    "component_lock_identity": (
                        prepared.locked_authority_snapshot.authority.lock.identity.uri
                    ),
                    "component_lock_identities": [
                        prepared.locked_authority_snapshot.authority.lock.identity.uri
                    ],
                    "specification": specification_label,
                    "target": getattr(args, "target", "host"),
                    "explicit_flavor_selectors": list(selectors),
                    "lifecycle_request_identity": (
                        rebuilt.lifecycle_request_identity.uri
                    ),
                    "lifecycle_invocation_identity": (
                        rebuilt.lifecycle_invocation_identity.uri
                    ),
                    "lifecycle_result_identity": (
                        rebuilt.execution.lifecycle.identity.uri
                    ),
                    "framework_distribution_identity": (
                        adapter.binding.distribution.identity.uri
                    ),
                    "toolchain_closure_identity": (
                        adapter.runtime.toolchain_closure.record.identity.uri
                    ),
                    "receipt_identity": rebuilt.receipt.identity.uri,
                    "source_cache_lifecycle_identity": (
                        rebuilt.finalized_receipt.source_cache_lifecycle_identity.uri
                    ),
                    "finalized_candidate_identity": (
                        rebuilt.finalized_receipt.identity.uri
                    ),
                    "receipt_committed": rebuilt.receipt_update is not None,
                    "receipt_update": rebuilt.receipt_update,
                    "candidate_receipt": None if candidate is None else str(candidate),
                    "coding_cli": rebuilt.execution.planned.coding_cli.name,
                    "dag": _standard_rebuild_dag(
                        prepared,
                        rebuilt.execution.planned.execution_plan,
                        rebuilt.execution.lifecycle,
                    ),
                    "source_generation": {
                        item.component_revision.uri: item.disposition.value
                        for item in rebuilt.execution.lifecycle.node_results
                    },
                    "build_cache": dict(rebuilt.execution.local_build_cache_report),
                    "repair": {"attempts": 0, "status": "not-needed"},
                    "test_summary": rebuilt.receipt.summary.to_dict(),
                    "artifact": str(artifact),
                    "observed_toolchain_identities": [
                        item.uri
                        for item in (
                            adapter.runtime.toolchain_closure.record.toolchain_identities
                        )
                    ],
                    "runtime_root": str(runtime_root),
                    "runtime_retained": (
                        not owns_runtime or getattr(args, "keep_runtime", False)
                    ),
                    "cache_directory_custody_identity": directories.identity.uri,
                    "build_dir": str(directories.build_dir),
                    "obj_dir": str(directories.obj_dir),
                }
                if execution_command is not None:
                    result["execution_command"] = execution_command.to_dict()
                if library_artifact is not None:
                    result["library_artifact"] = library_artifact
                if execution_entrypoints:
                    result["execution_entrypoints"] = execution_entrypoints
                return result
            except FilesystemStandardRebuildError as exc:
                raise CliFailure(
                    exc.code,
                    exc.message,
                    message_limit=(
                        8192
                        if exc.code == "standard_rebuild.independent_acceptance_failed"
                        else CLI_ERROR_MESSAGE_CHARS
                    ),
                ) from exc
            except (CodingCliError, RetainedSourceError) as exc:
                raise CliFailure(exc.code, exc.message) from exc
            except LocalStandardLifecycleError as exc:
                raise CliFailure(
                    getattr(
                        exc, "code", "standard_rebuild.independent_acceptance_failed"
                    ),
                    getattr(exc, "message", str(exc)),
                    message_limit=8192,
                ) from exc
            finally:
                if owns_runtime and not getattr(args, "keep_runtime", False):
                    shutil.rmtree(runtime_root)
    except ProjectLifecycleLockError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def _project(selected: str) -> LoadedProject:
    try:
        project = discover_project(Path(selected))
    except (OSError, ProjectError) as exc:
        if isinstance(exc, ProjectError):
            raise CliFailure(exc.code, exc.message) from exc
        raise CliFailure("project.not_found", "project root is unavailable") from exc
    if project is None:
        raise CliFailure(
            "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
        )
    return project


def _require_no_symlink_path(root: Path, target: Path, *, label: str) -> None:
    relative = target.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise CliFailure(
                "rebuild.path_unsafe", f"{label} cannot traverse a symbolic link"
            )


def _specification(project: LoadedProject, configured: str) -> tuple[Path, str]:
    source = Path(configured)
    candidate = source if source.is_absolute() else project.root / source
    try:
        target = candidate.resolve(strict=True)
    except OSError as exc:
        raise CliFailure(
            "rebuild.specification_unavailable",
            "rebuild specification entry point is unavailable",
        ) from exc
    if not target.is_relative_to(project.root) or not (
        target.is_file() or target.is_dir()
    ):
        raise CliFailure(
            "rebuild.specification_outside_project",
            "rebuild specification entry point must be inside the project",
        )
    _require_no_symlink_path(project.root, target, label="specification entry point")
    if target != project.root:
        component_roots = tuple(
            root.resolve(strict=True) for root in project.roots("component")
        )
        if not any(
            target == root or target.is_relative_to(root) for root in component_roots
        ):
            raise CliFailure(
                "rebuild.specification_outside_catalog",
                "rebuild specification entry point must be the project root or lie "
                "inside a declared Component root",
            )
    relative = target.relative_to(project.root).as_posix()
    return target, relative or "."


def _external_runtime_root(
    project: LoadedProject, configured: str, *, create: bool = True
) -> Path:
    candidate = Path(configured)
    if candidate.is_symlink():
        raise CliFailure(
            "rebuild.runtime_root_unsafe", "runtime root cannot be a symbolic link"
        )
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise CliFailure(
            "rebuild.runtime_root_unsafe", "runtime root parent must already exist"
        ) from exc
    if parent.is_symlink() or not parent.is_dir():
        raise CliFailure(
            "rebuild.runtime_root_unsafe",
            "runtime root parent must be a regular directory",
        )
    target = (parent / candidate.name).resolve()
    if target == project.root or target.is_relative_to(project.root):
        raise CliFailure(
            "rebuild.runtime_root_inside_project",
            "runtime root must be outside the project",
        )
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise CliFailure(
            "rebuild.runtime_root_not_empty",
            "runtime root must be new or an empty directory",
        )
    if not create:
        return target
    try:
        target.mkdir()
    except FileExistsError:
        pass
    except OSError as exc:
        raise CliFailure(
            "rebuild.runtime_root_unsafe", "runtime root could not be created"
        ) from exc
    try:
        if (
            target.is_symlink()
            or not target.is_dir()
            or target.resolve(strict=True) != target
        ):
            raise OSError
    except OSError as exc:
        raise CliFailure(
            "rebuild.runtime_root_unsafe",
            "runtime root changed while it was being prepared",
        ) from exc
    return target


def _external_candidate(project: LoadedProject, configured: str) -> Path:
    candidate = Path(configured)
    if candidate.is_symlink() or candidate.exists():
        raise CliFailure(
            "rebuild.candidate_exists",
            "candidate receipt path must not already exist",
        )
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise CliFailure(
            "rebuild.candidate_path_unsafe",
            "candidate receipt parent must already exist",
        ) from exc
    if parent.is_symlink() or not parent.is_dir():
        raise CliFailure(
            "rebuild.candidate_path_unsafe",
            "candidate receipt parent must be a regular directory",
        )
    target = (parent / candidate.name).resolve()
    if target == project.root or target.is_relative_to(project.root):
        raise CliFailure(
            "rebuild.candidate_inside_project",
            "candidate receipt must be written outside the project",
        )
    return target


def _commit_candidate_receipt(
    provisional: Path,
    destination: Path,
    *,
    expected_provisional: ProjectTestReceiptProvisional,
    source_cache_decision_identity: ContentIdentity,
    source_cache_lifecycle_identity: ContentIdentity,
) -> None:
    """Expose a complete candidate only after every outer side effect succeeded."""

    try:
        observed, _canonical_provisional = load_project_test_receipt_provisional(
            provisional
        )
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if observed != expected_provisional:
        raise CliFailure(
            "rebuild.provisional_receipt_changed",
            "provisional receipt changed after validation and outer side effects",
        )
    finalized = ProjectTestReceiptFinalizedCandidate.finalize(
        observed,
        source_cache_decision_identity=source_cache_decision_identity,
        source_cache_lifecycle_identity=source_cache_lifecycle_identity,
    )
    canonical = canonical_json_bytes(finalized.to_dict()) + b"\n"
    try:
        parent = destination.parent.resolve(strict=True)
        if (
            parent.is_symlink()
            or not parent.is_dir()
            or destination.is_symlink()
            or destination.exists()
        ):
            raise OSError
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".litai-candidate-", dir=parent
        )
    except OSError as exc:
        raise CliFailure(
            "rebuild.candidate_path_changed",
            "final candidate receipt path changed before commit",
        ) from exc
    temporary = Path(temporary_name)
    committed_identity: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary_stat = temporary.stat()
        committed_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        if (
            os.name == "nt"
        ):  # Windows rename fails rather than replacing an existing file.
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
        destination_stat = destination.stat()
        if (destination_stat.st_dev, destination_stat.st_ino) != committed_identity:
            raise OSError
        committed_candidate, committed_bytes = load_project_test_receipt_candidate(
            destination
        )
        if committed_candidate != finalized or committed_bytes != canonical:
            raise OSError
        _fsync_directory(parent)
    except (OSError, ProjectError) as exc:
        try:
            if destination.exists() and not destination.is_symlink():
                observed = destination.stat()
                if committed_identity == (observed.st_dev, observed.st_ino):
                    destination.unlink()
        except OSError:
            pass
        raise CliFailure(
            "rebuild.candidate_commit_failed",
            "validated receipt could not be atomically exposed at the final path",
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    try:
        provisional.unlink()
        _fsync_directory(provisional.parent)
    except OSError:
        # The envelope is structurally unpromotable, so cleanup failure cannot make
        # an incomplete lifecycle look finalized. Preserve it as diagnostics.
        pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _request_identity(
    *,
    project: LoadedProject,
    project_revision: ContentIdentity,
    specification: str,
    explicit_selectors: Sequence[str],
    driver: ProjectLifecycleDriver,
    executable_identity: ContentIdentity,
    implementation_identity: ContentIdentity,
    driver_environment: Mapping[str, object],
    source_cache_request: Mapping[str, object],
    component_lock_identities: Sequence[ContentIdentity],
) -> ContentIdentity:
    return canonical_identity(
        {
            "schema": PROJECT_REBUILD_REQUEST_SCHEMA,
            "project_id": project.definition.project_id,
            "project_revision_identity": project_revision.uri,
            "specification": specification,
            "project_default_flavor_selectors": list(
                project.definition.default_flavor_selectors
            ),
            "explicit_flavor_selectors": list(explicit_selectors),
            "driver_identity": driver.identity.uri,
            "driver_executable_identity": executable_identity.uri,
            "driver_implementation_identity": implementation_identity.uri,
            "driver_argv_template_identity": canonical_identity(driver.argv).uri,
            "driver_environment": dict(driver_environment),
            "source_cache_request": dict(source_cache_request),
            "component_lock_identities": [
                identity.uri for identity in component_lock_identities
            ],
            "phases": list(driver.phases),
            "host_execution_acknowledged": True,
        }
    )


def _planning_request_identity(
    *,
    project: LoadedProject,
    project_revision: ContentIdentity,
    specification: str,
    explicit_selectors: Sequence[str],
    driver: ProjectLifecycleDriver,
    executable_identity: ContentIdentity,
    implementation_identity: ContentIdentity,
    driver_environment: Mapping[str, object],
    source_cache_request: Mapping[str, object],
) -> ContentIdentity:
    """Bind the authority visible to the non-executing derivation planning pass."""

    return canonical_identity(
        {
            "schema": PROJECT_REBUILD_PLANNING_REQUEST_SCHEMA,
            "project_id": project.definition.project_id,
            "project_revision_identity": project_revision.uri,
            "specification": specification,
            "project_default_flavor_selectors": list(
                project.definition.default_flavor_selectors
            ),
            "explicit_flavor_selectors": list(explicit_selectors),
            "driver_identity": driver.identity.uri,
            "driver_executable_identity": executable_identity.uri,
            "driver_implementation_identity": implementation_identity.uri,
            "driver_argv_template_identity": canonical_identity(driver.argv).uri,
            "driver_environment": dict(driver_environment),
            "source_cache_request": dict(source_cache_request),
            "component_lock_reporting": {
                "required": True,
                "specification_scope": driver.specification_scope,
            },
            "phases": list(driver.phases),
            "mode": "read-only-derivation-planning",
            "host_execution_acknowledged": False,
        }
    )


def _component_acceptance_oracle(
    project_root: Path, specification: str, component_lock
):
    """Resolve the verifier-owned oracle for the Component being built.

    Generated source cannot be promoted on the generator's own word, so acceptance is
    required rather than optional for entrypoint kinds with defined verifier semantics.
    ``portable-application`` keeps its exact JSON invocation oracle and
    ``persistent-service`` uses the bounded HTTP process contract. Library and UI
    Components with no CLI invocation use a lint-and-render snapshot oracle when
    the verifier document is present; otherwise they remain explicitly exempt.
    The contract is looked up by Component name under the project's verifier root
    and is deliberately not referenced from `component.md`.
    """

    from literate_ai.adapters.component_acceptance import (
        ComponentAcceptanceError,
        resolve_component_acceptance_oracle,
    )

    try:
        return resolve_component_acceptance_oracle(
            project_root, specification, component_lock
        )
    except ComponentAcceptanceError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def _selected_cache_directories(project_root: Path, args: Any) -> CacheDirectories:
    """Bind cache custody: explicit flag, then environment, then portable default."""

    try:
        return select_cache_directories(
            project_root,
            build_dir=getattr(args, "build_dir", None),
            obj_dir=getattr(args, "obj_dir", None),
        )
    except (CacheDirectoryError, OSError) as exc:
        raise CliFailure("rebuild.cache_root_invalid", str(exc)) from exc


def rebuild_from_args(
    args: Any,
    *,
    cache_directories: CacheDirectories | None = None,
    standard_observer: Callable[[Any, Any, Any, Any], None] | None = None,
) -> dict[str, object]:
    """Run one project-authorized full lifecycle and validate its external receipt."""

    from literate_ai.diagnostics import debug_stage

    with debug_stage("rebuild", specification=str(getattr(args, "specification", "."))):
        return _rebuild_from_args_body(
            args,
            cache_directories=cache_directories,
            standard_observer=standard_observer,
        )


def _rebuild_from_args_body(
    args: Any,
    *,
    cache_directories: CacheDirectories | None = None,
    standard_observer: Callable[[Any, Any, Any, Any], None] | None = None,
) -> dict[str, object]:
    """Run one project-authorized full lifecycle and validate its external receipt."""

    retained_project = getattr(args, "retained_project", None)
    retained_project_plan = getattr(args, "retained_project_plan", False)
    if not retained_project and any(
        getattr(args, key, None)
        for key in (
            "retained_project_profile",
            "retained_project_plan",
            "authorize_retained_project",
        )
    ):
        raise CliFailure(
            "retained_project.manifest_required",
            "Retained-project options require an explicit manifest",
        )
    retained_plan = getattr(args, "retained_source_plan", False)
    retained_path = getattr(args, "retained_source", None)
    if (
        retained_plan or getattr(args, "authorize_retained_source", None)
    ) and not retained_path:
        raise CliFailure(
            "retained_source.path_required",
            "Retained-source options require --retained-source",
        )
    if (
        not args.allow_host_execution
        and not retained_plan
        and not retained_project_plan
    ):
        raise CliFailure(
            "rebuild.host_execution_not_acknowledged",
            "a full rebuild compiles and executes generated host code; rerun with "
            "--allow-host-execution after reviewing the selected specification, "
            "Flavors, skills, and lifecycle driver",
        )
    project = _project(args.project)
    from literate_ai.adapters.conversion_authority import (
        ConversionAuthorityError,
        FilesystemConversionAuthorityStore,
        require_current_qualified_conversion_authority,
    )
    from literate_ai.contracts.operator_adoption import ConversionAuthorityStage

    try:
        conversion_authority = FilesystemConversionAuthorityStore(
            project.root
        ).load_optional()
    except ConversionAuthorityError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if retained_project:
        from .retained_project import retained_project_from_args

        driver = project.definition.lifecycle_driver
        if not isinstance(driver, StandardProjectLifecycleDriver):
            raise CliFailure(
                "retained_project.standard_required",
                "Retained-project admission requires the Standard lifecycle",
            )
        return retained_project_from_args(
            project, _resolve_standard_binding(driver), args
        )
    if (
        conversion_authority is not None
        and conversion_authority.stage is not ConversionAuthorityStage.QUALIFIED
    ):
        raise CliFailure(
            "rebuild.conversion_authority_not_qualified",
            "adopted project remains original-source authoritative at conversion stage "
            f"{conversion_authority.stage.value!r}; complete retained evidence, draft "
            "Component specifications, and regenerative qualification before using "
            "litai rebuild as release authority",
        )
    if conversion_authority is not None:
        try:
            require_current_qualified_conversion_authority(
                project, conversion_authority
            )
        except ConversionAuthorityError as exc:
            raise CliFailure(exc.code, exc.message) from exc
    try:
        project_index = require_lifecycle_project_index(
            project.root,
            project.definition.source_intelligence,
            stage=SourceIntelligenceStage.SOURCE_GENERATION,
        )
    except ProjectSourceIntelligenceError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    driver = project.definition.lifecycle_driver
    if driver is None:
        raise CliFailure(
            "rebuild.driver_unconfigured",
            "project manifest must configure a trusted lifecycle_driver before "
            "litai rebuild can execute the full SDLC",
        )
    if isinstance(driver, StandardProjectLifecycleDriver):
        result = _standard_rebuild_from_args(
            project,
            _resolve_standard_binding(driver),
            args,
            cache_directories=cache_directories,
            observer=standard_observer,
        )
        return {**result, "project_source_intelligence": project_index}
    if retained_path:
        raise CliFailure(
            "retained_source.standard_required",
            "Retained source requires the Standard lifecycle",
        )
    if standard_observer is not None:
        raise CliFailure(
            "package.standard_driver_required",
            "native package construction currently requires the Standard lifecycle "
            "driver so accepted package custody remains available",
        )
    if not isinstance(driver, ProjectLifecycleDriver):
        raise CliFailure(
            "rebuild.driver_unsupported",
            "the selected lifecycle driver binding is not supported by this CLI",
        )
    if getattr(args, "from_accepted_source", False):
        raise CliFailure(
            "rebuild.accepted_source_only_unsupported",
            "--from-accepted-source requires the Standard lifecycle driver; "
            "external lifecycle drivers do not carry accepted-source-only authority",
        )
    if args.runtime_root is None or args.candidate_receipt is None:
        raise CliFailure(
            "rebuild.external_paths_required",
            "external lifecycle drivers require --runtime-root and --candidate-receipt",
        )
    if getattr(args, "update_receipt", False):
        raise CliFailure(
            "rebuild.external_update_receipt_unsupported",
            "external lifecycle candidates must be promoted with "
            "litai project test-receipt update",
        )
    if project.definition.test_receipt_policy is None:
        raise CliFailure(
            "rebuild.receipt_policy_unconfigured",
            "project manifest must configure a test receipt policy before rebuild",
        )
    specification, specification_label = _specification(project, args.specification)
    if driver.specification_scope == "project" and specification != project.root:
        raise CliFailure(
            "rebuild.specification_scope_mismatch",
            "configured lifecycle driver rebuilds the whole project; use '.' as the "
            "specification entry point",
        )
    if driver.specification_scope == "component" and specification == project.root:
        raise CliFailure(
            "rebuild.specification_scope_mismatch",
            "configured lifecycle driver requires one Component/specification "
            "entry point",
        )
    explicit_selectors = tuple(
        flavor_selector(value, f"rebuild Flavor selector[{index}]")
        for index, value in enumerate(args.flavor)
    )
    selectors = (
        explicit_selectors
        if specification == project.root
        else project.flavor_selectors_for(specification, explicit_selectors)
    )
    prospective_runtime_root = _external_runtime_root(
        project, args.runtime_root, create=False
    )
    cache_inputs = parse_rebuild_source_cache_inputs(
        project,
        force_regeneration=args.force_regeneration,
        entry_values=args.source_cache_entry,
        root_values=args.source_cache_root,
        standard_local_root=prospective_runtime_root / "source-cache",
    )
    if "publish-source-cache" in driver.phases and (
        project.definition.source_cache is None
        or not project.definition.source_cache.mode.can_write
    ):
        raise CliFailure(
            "rebuild.source_cache_publication_unconfigured",
            "publish-source-cache requires a configured cache write mode",
        )
    runtime_root = _external_runtime_root(project, args.runtime_root)
    if runtime_root != prospective_runtime_root:
        raise CliFailure(
            "rebuild.runtime_root_changed",
            "runtime root changed between validation and allocation",
        )
    candidate_receipt = _external_candidate(project, args.candidate_receipt)
    validated_project_revision = validated_project_authority_identity(project.root)
    if cache_directories is None:
        cache_directories = _selected_cache_directories(project.root, args)
    try:
        # Overlay the selected roots before binding, so the driver's environment and the
        # identity material derived from it describe the same custody.
        binding = bind_external_project_lifecycle_driver(
            project,
            driver,
            {**os.environ, **cache_directories.environment()},
        )
    except ProjectLifecycleDriverAdapterError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    environment = dict(binding.environment)
    driver_environment = dict(binding.environment_identity_material)
    executable_identity = binding.executable_identity
    implementation_identity = binding.implementation_identity
    planning_request_identity = _planning_request_identity(
        project=project,
        project_revision=validated_project_revision,
        specification=specification_label,
        explicit_selectors=selectors,
        driver=driver,
        executable_identity=executable_identity,
        implementation_identity=implementation_identity,
        driver_environment=driver_environment,
        source_cache_request=cache_inputs.identity_material(project),
    )
    planning_protocol = prepare_rebuild_source_cache_planning_protocol(
        runtime_root=runtime_root
    )
    try:
        planning_argv = binding.command(
            specification=specification,
            runtime_root=runtime_root,
            candidate_receipt=(
                planning_protocol.manifest_path.parent / "planning-receipt.unused.json"
            ),
            project_revision=validated_project_revision,
            request_identity=planning_request_identity,
            flavor_selectors=selectors,
            allow_host_execution=False,
        )
    except ProjectLifecycleDriverAdapterError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    planning_environment = dict(environment)
    planning_environment["LITAI_LIFECYCLE_REQUEST_IDENTITY"] = (
        planning_request_identity.uri
    )
    planning_environment["LITAI_LIFECYCLE_COMMAND_IDENTITY"] = canonical_identity(
        tuple(planning_argv)
    ).uri
    try:
        planning_protocol.add_environment(
            planning_environment,
            planning_request_identity=planning_request_identity,
        )
        binding.run(
            planning_argv,
            environment=planning_environment,
            timeout_seconds=min(driver.timeout_seconds, 300),
        )
        planning_protocol.require_directories_unchanged()
        derivation_manifest = planning_protocol.load_manifest()
    except (
        CliFailure,
        ProjectLifecycleDriverAdapterError,
        RebuildSourceCacheProtocolError,
    ) as exc:
        raise CliFailure(
            "rebuild.derivation_planning_failed",
            "configured lifecycle driver did not complete the required bounded, "
            "non-executing derivation planning pass",
        ) from exc
    try:
        binding.require_executable_unchanged()
        current_implementation_identity = binding.current_implementation_identity()
    except ProjectLifecycleDriverAdapterError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if current_implementation_identity != implementation_identity:
        raise CliFailure(
            "rebuild.driver_implementation_changed",
            "lifecycle driver implementation changed during derivation planning",
        )
    if (
        derivation_manifest.planning_request_identity != planning_request_identity
        or derivation_manifest.lifecycle_driver_identity != driver.identity
    ):
        raise CliFailure(
            "rebuild.derivation_manifest_mismatch",
            "derivation manifest does not bind the exact outer planning request, "
            "and lifecycle driver",
        )
    component_lock_identities = derivation_manifest.component_lock_identities
    if (
        driver.specification_scope == "component"
        and len(component_lock_identities) != 1
    ):
        raise CliFailure(
            "rebuild.component_lock_scope_mismatch",
            "component-scoped planning must report exactly one Component lock",
        )
    project_revision = rebuild_project_authority_identity(
        validated_project_revision,
        component_lock_identities,
    )
    if derivation_manifest.project_revision_identity != project_revision:
        raise CliFailure(
            "rebuild.derivation_manifest_mismatch",
            "derivation manifest does not bind the validated project authority and "
            "exact planned Component lock set",
        )
    if (
        validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        != validated_project_revision
    ):
        raise CliFailure(
            "rebuild.project_changed",
            "project authority changed during derivation planning",
        )
    source_cache_request = {
        **cache_inputs.identity_material(project),
        "derivation_manifest_identity": derivation_manifest.identity.uri,
        "lifecycle_plan_identity": derivation_manifest.lifecycle_plan_identity.uri,
    }
    request_identity = _request_identity(
        project=project,
        project_revision=project_revision,
        specification=specification_label,
        explicit_selectors=selectors,
        driver=driver,
        executable_identity=executable_identity,
        implementation_identity=implementation_identity,
        driver_environment=driver_environment,
        source_cache_request=source_cache_request,
        component_lock_identities=component_lock_identities,
    )
    cache_protocol = prepare_rebuild_source_cache_protocol(
        project,
        planning_protocol=planning_protocol,
        derivation_manifest=derivation_manifest,
        lifecycle_request_identity=request_identity,
        project_revision_identity=project_revision,
        inputs=cache_inputs,
    )
    provisional_receipt = (
        cache_protocol.control_path.parent / _PROVISIONAL_RECEIPT_FILENAME
    )
    if provisional_receipt.exists() or provisional_receipt.is_symlink():
        raise CliFailure(
            "rebuild.provisional_receipt_exists",
            "external runtime already contains a provisional receipt path",
        )
    try:
        argv = binding.command(
            specification=specification,
            runtime_root=runtime_root,
            candidate_receipt=provisional_receipt,
            project_revision=project_revision,
            request_identity=request_identity,
            flavor_selectors=selectors,
        )
    except ProjectLifecycleDriverAdapterError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    command_identity = canonical_identity(tuple(argv))
    environment["LITAI_LIFECYCLE_COMMAND_IDENTITY"] = command_identity.uri
    environment["LITAI_LIFECYCLE_REQUEST_IDENTITY"] = request_identity.uri
    try:
        cache_protocol.add_environment(environment)
    except RebuildSourceCacheProtocolError as exc:
        raise CliFailure(
            "rebuild.source_cache_protocol_changed",
            "external source-cache protocol changed before driver execution",
        ) from exc
    try:
        binding.run(
            argv,
            environment=environment,
            timeout_seconds=driver.timeout_seconds,
        )
        binding.require_executable_unchanged()
        current_implementation_identity = binding.current_implementation_identity()
    except ProjectLifecycleDriverAdapterError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if current_implementation_identity != implementation_identity:
        raise CliFailure(
            "rebuild.driver_implementation_changed",
            "lifecycle driver implementation changed during rebuild",
        )
    try:
        cache_protocol.require_directories_unchanged()
    except RebuildSourceCacheProtocolError as exc:
        raise CliFailure(
            "rebuild.runtime_root_changed",
            "external runtime or source-cache protocol directory changed during "
            "rebuild",
        ) from exc
    if (
        validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        != validated_project_revision
    ):
        raise CliFailure(
            "rebuild.project_changed",
            "project authority changed during rebuild; discard the candidate and retry",
        )
    current_project = _project(str(project.root))
    if (
        current_project.definition.lifecycle_driver is None
        or current_project.definition.lifecycle_driver.identity != driver.identity
    ):
        raise CliFailure(
            "rebuild.project_changed",
            "project lifecycle driver changed during rebuild",
        )
    try:
        provisional = validate_project_test_receipt_provisional(
            current_project,
            provisional_receipt,
            project_revision_identity=project_revision,
            lifecycle_request_identity=request_identity,
            lifecycle_command_identity=command_identity,
            source_cache_control_identity=cache_protocol.control.identity,
            component_lock_identities=component_lock_identities,
        )
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    receipt = provisional.receipt
    evidence = {item.kind: item.identity for item in receipt.evidence}
    if evidence.get("lifecycle-request") != request_identity:
        raise CliFailure(
            "rebuild.receipt_request_mismatch",
            "candidate receipt does not bind the exact rebuild request",
        )
    if evidence.get("lifecycle-command") != command_identity:
        raise CliFailure(
            "rebuild.receipt_command_mismatch",
            "candidate receipt does not bind the exact expanded lifecycle command",
        )
    cache_decision = validate_rebuild_source_cache_decision(
        cache_protocol,
        receipt_evidence=evidence,
    )
    cache_lifecycle = validate_rebuild_source_cache_lifecycle(
        cache_protocol,
        cache_decision,
        receipt,
    )
    missing = _FULL_REBUILD_EVIDENCE - evidence.keys()
    if missing:
        raise CliFailure(
            "rebuild.receipt_incomplete",
            "candidate receipt does not prove the complete lifecycle: "
            + ", ".join(sorted(missing)),
        )
    extension_evidence = {
        "package-artifacts": "package-result",
        "publish-source-cache": "source-cache-publication-result",
        "publish-artifacts": "artifact-publication-result",
        "deploy": "deployment-result",
    }
    missing_extensions = {
        extension_evidence[phase]
        for phase in driver.phases
        if phase in PROJECT_LIFECYCLE_EXTENSION_PHASES
        and extension_evidence[phase] not in evidence
    }
    if missing_extensions:
        raise CliFailure(
            "rebuild.receipt_extension_incomplete",
            "candidate receipt does not prove configured lifecycle extensions: "
            + ", ".join(sorted(missing_extensions)),
        )
    if (
        validated_project_authority_identity(
            current_project.root, synchronize_source_intelligence=False
        )
        != validated_project_revision
    ):
        raise CliFailure(
            "rebuild.project_changed",
            "project authority changed while the candidate receipt was validated",
        )
    published_cache_entries = publish_rebuild_source_cache_offer(
        current_project,
        cache_protocol,
        cache_decision,
        cache_lifecycle,
        receipt,
        phases=driver.phases,
    )
    try:
        cache_protocol.require_directories_unchanged()
    except RebuildSourceCacheProtocolError as exc:
        raise CliFailure(
            "rebuild.runtime_root_changed",
            "external runtime or source-cache protocol directory changed before "
            "candidate finalization",
        ) from exc
    _commit_candidate_receipt(
        provisional_receipt,
        candidate_receipt,
        expected_provisional=provisional,
        source_cache_decision_identity=cache_decision.identity,
        source_cache_lifecycle_identity=cache_lifecycle.identity,
    )
    return {
        "schema": PROJECT_REBUILD_RESULT_SCHEMA,
        "passed": True,
        "project_id": project.definition.project_id,
        "project_revision_identity": project_revision.uri,
        "validated_project_authority_identity": validated_project_revision.uri,
        "component_lock_identities": [
            identity.uri for identity in component_lock_identities
        ],
        "specification": specification_label,
        "target": getattr(args, "target", "host"),
        "project_default_flavor_selectors": list(
            project.definition.default_flavor_selectors
        ),
        "explicit_flavor_selectors": list(selectors),
        "lifecycle_request_identity": request_identity.uri,
        "lifecycle_command_identity": command_identity.uri,
        "driver_executable_identity": executable_identity.uri,
        "driver_implementation_identity": implementation_identity.uri,
        "lifecycle_driver_identity": driver.identity.uri,
        "source_cache_control_identity": cache_protocol.control.identity.uri,
        "source_cache_decision_identity": cache_decision.identity.uri,
        "source_cache_lifecycle_identity": cache_lifecycle.identity.uri,
        "source_cache_mode": cache_decision.mode,
        "source_cache_fresh_generation_required": (
            cache_decision.fresh_generation_required
        ),
        "source_cache_published_entry_identities": [
            item.uri for item in published_cache_entries
        ],
        "subject_identity": receipt.subject_identity.uri,
        "receipt_identity": receipt.identity.uri,
        "provisional_receipt_identity": provisional.identity.uri,
        "finalized_candidate_identity": ProjectTestReceiptFinalizedCandidate.finalize(
            provisional,
            source_cache_decision_identity=cache_decision.identity,
            source_cache_lifecycle_identity=cache_lifecycle.identity,
        ).identity.uri,
        "runtime_root": str(runtime_root),
        "candidate_receipt": str(candidate_receipt),
        "cache_directory_custody_identity": cache_directories.identity.uri,
        "build_dir": str(cache_directories.build_dir),
        "obj_dir": str(cache_directories.obj_dir),
        "receipt_committed": False,
        "phases": list(driver.phases),
        "lifecycle_extensions": [
            phase
            for phase in driver.phases
            if phase in PROJECT_LIFECYCLE_EXTENSION_PHASES
        ],
        "project_source_intelligence": project_index,
    }


__all__ = [
    "PROJECT_REBUILD_REQUEST_SCHEMA",
    "PROJECT_REBUILD_RESULT_SCHEMA",
    "rebuild_from_args",
]
