"""Canonical project initialization and validation commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from literate_ai.adapters.component_lock_application import (
    ProjectComponentLockSetError,
    current_rebuild_project_authority_identity,
)
from literate_ai.adapters.documentation_update import (
    DocumentationUpdateError,
    apply_documentation_update,
    plan_documentation_update,
)
from literate_ai.adapters.execution_dispatch import (
    ExecutionDispatchAdapterError,
    load_execution_worker_catalog,
)
from literate_ai.adapters.harness_inventory import (
    HARNESS_COMMAND_TIMEOUT_SECONDS,
    HARNESS_DIAGNOSTIC_CHARS,
    HARNESS_DIAGNOSTIC_ENVIRONMENT,
)
from literate_ai.adapters.harness_workspace import (
    HarnessWorkspaceError,
    harness_workspace_link_evidence,
    resolve_harness_workspace_links,
)
from literate_ai.adapters.mac_project_contract import (
    MacProjectContractError,
    write_mac_repository_contract,
)
from literate_ai.adapters.project_initialization import (
    KNOWN_FLAVOR_SELECTORS,
    FilesystemProjectInitializationAdapter,
    ProjectInitializationError,
    apply_init_flavor_defaults,
    detect_repo_flavors,
    plan_convert,
    record_project_authority_review,
)
from literate_ai.adapters.project_reparenting import (
    FilesystemRepositoryReparentAdapter,
    RepositoryReparentError,
)
from literate_ai.adapters.project_update_apply import apply_project_update
from literate_ai.adapters.project_update_conflicts import (
    enrich_conflict_files,
    framework_conflict_diffs,
    lineage_conflict_diffs,
)
from literate_ai.adapters.project_update_reviews import (
    ConflictReviewError,
    review_update_conflicts,
)
from literate_ai.adapters.project_update_work_items import (
    DEFAULT_QUEUE_PATH,
    ProjectUpdateWorkItemError,
    record_work_items,
)
from literate_ai.adapters.project_updates import (
    FilesystemProjectUpdateAdapter,
    ProjectUpdateError,
)
from literate_ai.adapters.project_validation import (
    PROJECT_VALIDATION_SCHEMA,
    FilesystemProjectValidationAdapter,
    ProjectValidationError,
    validate_project,
)
from literate_ai.adapters.project_validation import (
    validated_project_authority_identity as adapter_project_authority_identity,
)
from literate_ai.adapters.repository_catalogs import plan_inherited_catalogs
from literate_ai.adapters.repository_lineage import (
    FilesystemRepositoryLineageStore,
    GitRepositoryLineageError,
    GitRepositorySnapshotProvider,
    RepositoryLineageStoreError,
    repository_parent_reference,
)
from literate_ai.adapters.repository_updates import (
    FilesystemRepositoryUpdateAdapter,
    RepositoryUpdateError,
)
from literate_ai.adapters.retained_evidence_migration import (
    migrate_retained_evidence,
)
from literate_ai.adapters.retained_harness_receipts import (
    RetainedHarnessReceiptError,
    load_known_failure_report,
    run_retained_harness_receipt,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    observe_installed_framework_distribution,
    observe_installed_framework_origin,
)
from literate_ai.adapters.user_assets import (
    UserAssetPathError,
    resolve_worker_config_path,
)
from literate_ai.application.project_update_work_items import (
    project_update_work_items,
)
from literate_ai.application.project_updates import ProjectUpdateService
from literate_ai.application.repository_lineage import resolve_repository_lineage
from literate_ai.application.repository_parent_follow import (
    is_exact_git_object_id,
    next_release_line,
    parent_selector_kind,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContractValidationError,
    ExecutionWorker,
    ExecutionWorkerKind,
    ProjectUpdateClassification,
    RepositoryParentReference,
    RepositoryParentSelection,
    SourceIntelligenceStage,
    SourceIntelligenceStageStatus,
)
from literate_ai.diagnostics import report_progress
from literate_ai.project_source_index import (
    CodeGraphProjectSourceIntelligence,
    ProjectSourceIntelligenceError,
)
from literate_ai.projects import (
    PROJECT_FILENAME,
    ProjectError,
    discover_project,
    validate_project_structure,
)
from literate_ai.test_receipts import (
    check_project_test_receipt,
    require_current_project_test_receipt,
    update_project_test_receipt,
)

from .errors import CLI_ERROR_MESSAGE_CHARS, CliFailure
from .repository_fetch_deadlines import repository_fetch_deadlines_from_args

PROJECT_INITIALIZATION_SCHEMA = "literate-ai/project-initialization@6"


def project_lifecycle_from_args(args) -> dict[str, Any]:
    """Plan or apply a reviewed Standard lifecycle distribution rebind."""

    from literate_ai.application.standard_lifecycle_rebind import (
        StandardLifecycleRebindError,
        apply_standard_lifecycle_rebind,
        plan_standard_lifecycle_rebind,
    )

    try:
        if args.apply:
            if args.output is not None:
                raise CliFailure(
                    "standard_rebind.output_unexpected",
                    "--output is accepted only while planning",
                )
            if args.plan is None:
                raise CliFailure(
                    "standard_rebind.plan_required",
                    "--apply requires the reviewed rebind plan path",
                )
            plan_path = Path(args.plan)
            if plan_path.is_symlink() or not plan_path.is_file():
                raise CliFailure(
                    "standard_rebind.plan_unavailable",
                    "reviewed rebind plan is unavailable or unsafe",
                )
            try:
                raw = plan_path.read_bytes()
                if not raw or len(raw) > 1024 * 1024:
                    raise ValueError
                plan = json.loads(raw)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise CliFailure(
                    "standard_rebind.plan_invalid",
                    "reviewed rebind plan is invalid",
                ) from exc
            if not isinstance(plan, dict):
                raise CliFailure(
                    "standard_rebind.plan_invalid",
                    "reviewed rebind plan must be a JSON object",
                )
            return apply_standard_lifecycle_rebind(
                Path(args.project),
                plan,
                authorize_rebind=args.authorize_rebind,
                distribution_observer=observe_installed_framework_distribution,
                origin_observer=observe_installed_framework_origin,
            )
        if args.plan is not None:
            raise CliFailure(
                "standard_rebind.plan_unexpected",
                "a plan path is accepted only with --apply",
            )
        if args.authorize_rebind:
            raise CliFailure(
                "standard_rebind.authorization_unexpected",
                "--authorize-rebind is accepted only with --apply",
            )
        result = plan_standard_lifecycle_rebind(
            Path(args.project),
            distribution_observer=observe_installed_framework_distribution,
            origin_observer=observe_installed_framework_origin,
        )
        if args.output is not None:
            output = Path(args.output)
            try:
                parent = output.parent.resolve(strict=True)
                if output.exists() or output.is_symlink() or not parent.is_dir():
                    raise OSError
                descriptor = os.open(
                    parent / output.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(result, stream, indent=2)
                    stream.write("\n")
            except OSError as exc:
                raise CliFailure(
                    "standard_rebind.output_unavailable",
                    "rebind plan output must be a new file in an existing directory",
                ) from exc
            result["output"] = str(parent / output.name)
        return result
    except StandardLifecycleRebindError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def _repository_fetch_provider(args) -> GitRepositorySnapshotProvider:
    configured = os.environ.get("OBJ_DIR")
    object_root = (
        Path(configured).expanduser() if configured else Path.cwd() / "_build"
    ).resolve()
    deadlines = repository_fetch_deadlines_from_args(args)
    return GitRepositorySnapshotProvider(
        object_root / "repository-lineage",
        deadline_policy=deadlines.policy,
        deadline_provenance=deadlines.provenance,
        deadline_field_provenance=deadlines.field_provenance,
    )


def _flavor_diagnostic() -> str:
    lines = [
        "  +" + name + " (" + axis + ")"
        for name, axis in sorted(KNOWN_FLAVOR_SELECTORS.items())
    ]
    return "known selectors:\n" + "\n".join(lines)


def init_project_from_args(args) -> dict[str, Any]:
    convert = getattr(args, "convert", False)
    convert_plan = getattr(args, "convert_plan", False)
    run_baseline = getattr(args, "run_baseline", False)
    configured_baseline_timeout = getattr(args, "baseline_timeout_seconds", None)
    if configured_baseline_timeout is not None and not convert:
        raise CliFailure(
            "project.convert_timeout_requires_convert",
            "--baseline-timeout-seconds requires --convert",
        )
    baseline_timeout_seconds = (
        HARNESS_COMMAND_TIMEOUT_SECONDS
        if configured_baseline_timeout is None
        else configured_baseline_timeout
    )
    configured_diagnostic_chars = getattr(args, "baseline_diagnostic_chars", None)
    if configured_diagnostic_chars is not None and not convert:
        raise CliFailure(
            "project.convert_diagnostic_limit_requires_convert",
            "--baseline-diagnostic-chars requires --convert",
        )
    environment_diagnostic_chars = (
        os.environ.get(HARNESS_DIAGNOSTIC_ENVIRONMENT, "").strip()
        if convert and configured_diagnostic_chars is None
        else ""
    )
    if environment_diagnostic_chars:
        try:
            baseline_diagnostic_chars = int(environment_diagnostic_chars)
        except ValueError as exc:
            raise CliFailure(
                "project.convert_diagnostic_limit_invalid",
                f"{HARNESS_DIAGNOSTIC_ENVIRONMENT} must be an integer",
            ) from exc
    else:
        baseline_diagnostic_chars = (
            HARNESS_DIAGNOSTIC_CHARS
            if configured_diagnostic_chars is None
            else configured_diagnostic_chars
        )
    raw_workspace_links = tuple(getattr(args, "harness_workspace_link", ()))
    if raw_workspace_links and not convert:
        raise CliFailure(
            "harness.workspace_links_require_convert",
            "--harness-workspace-link requires --convert",
        )
    try:
        harness_workspace_links = (
            resolve_harness_workspace_links(
                raw_workspace_links,
                base=Path.cwd(),
                project_root=Path(args.path),
            )
            if raw_workspace_links
            else ()
        )
    except HarnessWorkspaceError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    allow_unready = getattr(args, "allow_unready", False)
    source_intelligence_provider = getattr(args, "source_intelligence_provider", "none")
    from literate_ai.diagnostics import debug_stage

    stage_name = "convert" if convert else "init"
    if source_intelligence_provider not in {"none", "codegraph-cli"}:
        raise CliFailure(
            "project.init_source_intelligence_unsupported",
            "source-intelligence provider must be none or codegraph-cli",
        )
    if convert_plan:
        if not convert:
            raise CliFailure(
                "project.convert_plan_requires_convert",
                "litai init --plan requires --convert",
            )
        try:
            with debug_stage(stage_name, path=str(args.path)):
                report = plan_convert(
                    Path(args.path),
                    default_branch=getattr(args, "default_branch", None),
                )
                if harness_workspace_links:
                    report["workspace_links"] = harness_workspace_link_evidence(
                        harness_workspace_links
                    )
                return report
        except ProjectInitializationError as exc:
            raise CliFailure(exc.code, exc.message) from exc
    flavor_selectors: tuple[str, ...] | None = None
    if hasattr(args, "flavors"):
        # Called from the `litai init` command context (not the promote flow).
        raw = args.flavors or []
        if convert and not raw:
            # Auto-detect from the repo when --convert is given without --flavor.
            raw = detect_repo_flavors(Path(args.path))
        unknown = [f for f in raw if f.lstrip("+") not in KNOWN_FLAVOR_SELECTORS]
        if unknown:
            raise CliFailure(
                "project.init_flavor_unknown",
                "unknown flavor selector(s): "
                + ", ".join(repr(f) for f in unknown)
                + "; "
                + _flavor_diagnostic(),
            )
        flavor_selectors = apply_init_flavor_defaults(raw)
    repository_from = getattr(args, "repository_from", None)
    parent_selection = getattr(args, "parent_selection", None)
    if parent_selection is not None and not isinstance(
        parent_selection, RepositoryParentSelection
    ):
        raise CliFailure(
            "project.init_parent_selection_invalid",
            "internal parent selection must be a RepositoryParentSelection",
        )
    if repository_from is not None:
        if parent_selection is not None:
            raise CliFailure(
                "project.init_parent_selection_conflict",
                "an internal parent selection cannot be combined with --from",
            )
        try:
            parent_selection = RepositoryParentSelection.inherit(
                (repository_parent_reference(repository_from, base=Path.cwd()),)
            )
        except GitRepositoryLineageError as exc:
            raise CliFailure(exc.code, exc.message) from exc
    provider = _repository_fetch_provider(args)
    try:
        with debug_stage(stage_name, path=str(args.path)):
            initialized = FilesystemProjectInitializationAdapter(
                repository_lineage_resolver=lambda selection: (
                    resolve_repository_lineage(selection, provider)
                ),
                repository_catalog_planner=lambda lineage: plan_inherited_catalogs(
                    lineage, provider
                ),
            ).initialize(
                Path(args.path),
                project_id=args.project_id,
                profile=args.profile,
                source_intelligence_provider=source_intelligence_provider,
                empty=getattr(args, "empty", False),
                project_type=getattr(args, "project_type", None),
                bootstrap_tools=not getattr(args, "no_tool_bootstrap", False),
                flavor_selectors=flavor_selectors,
                convert=convert,
                run_baseline=run_baseline,
                baseline_timeout_seconds=baseline_timeout_seconds,
                baseline_diagnostic_chars=baseline_diagnostic_chars,
                harness_workspace_links=harness_workspace_links,
                allow_unready=allow_unready,
                parent_selection=parent_selection,
                default_branch=getattr(args, "default_branch", None),
                root_plan=(
                    Path(args.root_plan).expanduser()
                    if getattr(args, "root_plan", None) is not None
                    else None
                ),
            )
    except ProjectInitializationError as exc:
        if convert and exc.code in {
            "project.convert_ci_failed",
            "project.convert_legacy_gate_failed",
        }:
            raise CliFailure(
                exc.code,
                exc.message,
                message_limit=baseline_diagnostic_chars + CLI_ERROR_MESSAGE_CHARS,
            ) from exc
        raise CliFailure(exc.code, exc.message) from exc
    return {
        "schema": PROJECT_INITIALIZATION_SCHEMA,
        "project_id": initialized["project_id"],
        "project_identity": initialized["project_identity"],
        "project_authority_identity": initialized["project_authority_identity"],
        "profile": initialized["profile"],
        "project_type": initialized.get("project_type"),
        "source_intelligence": initialized["source_intelligence"],
        "tool_bootstrap": initialized["tool_bootstrap"],
        "prerequisites": initialized.get("prerequisites"),
        "created": initialized["created"],
        "preserved": initialized.get("preserved", []),
        "detected_languages": initialized.get("detected_languages", []),
        "initialization_origin": initialized["initialization_origin"],
        "initialization_baseline": initialized["initialization_baseline"],
        "repository_parent": initialized["repository_parent"],
        "repository_lineage": initialized["repository_lineage"],
        "repository_fetch_deadline": provider.deadline_evidence,
        "inherited_catalogs": initialized["inherited_catalogs"],
        "catalogs": initialized["catalogs"],
        "initial_lock": initialized["initial_lock"],
        "standard_binding": initialized["standard_binding"],
        "test_matrix": initialized["test_matrix"],
        "monorepo_components": initialized.get("monorepo_components"),
        "conversion_authority": initialized.get("conversion_authority"),
    }


def validate_project_from_args(args) -> dict[str, Any]:
    try:
        return validate_project(
            Path(args.path),
            require_authority_review=True,
            synchronize_source_intelligence=False,
        )
    except ProjectValidationError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def mac_project_contract_from_args(args) -> dict[str, Any]:
    """Project MAC runner authority without resolving dependencies a second time."""

    loaded = discover_project(Path(args.project))
    if loaded is None:
        raise CliFailure(
            "project.not_found", f"no {PROJECT_FILENAME} found from {args.project}"
        )
    try:
        result = write_mac_repository_contract(
            Path(args.resolved_bom),
            project_root=loaded.root,
            project=loaded.definition.project_id,
            platforms=tuple(args.platform),
            bootstrap_command=args.bootstrap_command,
            test_command=args.test_command,
            bootstrap_creates=tuple(args.bootstrap_creates),
        )
    except MacProjectContractError as exc:
        raise CliFailure(exc.code, str(exc)) from exc
    return {
        **result,
        "project": loaded.definition.project_id,
        "written": [".mac/project.yaml", ".mac/project.contract.json"],
    }


def _parent_selector_reports(lineage) -> list[dict[str, object]]:
    selected = {item.repository_url for item in lineage.selection.parents}
    reports = []
    for node in lineage.nodes:
        if node.repository_url not in selected:
            continue
        kind = parent_selector_kind(node.requested_revision)
        reports.append(
            {
                "repository_url": node.repository_url,
                "requested_revision": node.requested_revision,
                "resolved_revision": node.resolved_revision,
                "kind": kind,
                "pinned": kind == "exact-commit",
            }
        )
    return reports


def _requested_follow_revision(
    args, parent, provider: GitRepositorySnapshotProvider
) -> str:
    follow_ref = getattr(args, "follow_ref", None)
    unpin = bool(getattr(args, "unpin", False))
    allow_major = bool(getattr(args, "allow_major", False))
    selected = sum(bool(item) for item in (follow_ref, unpin, allow_major))
    if selected == 0:
        return parent.requested_revision
    if selected > 1:
        raise CliFailure(
            "cli.usage",
            "use only one of --follow-ref, --unpin, or --allow-major",
        )
    if unpin:
        if not is_exact_git_object_id(parent.requested_revision):
            raise CliFailure(
                "repository_update.parent_not_pinned",
                "parent requested_revision is already a moving selector; "
                "use --follow-ref to change it",
            )
        return "HEAD"
    if follow_ref:
        try:
            return RepositoryParentReference(
                parent.repository_url, str(follow_ref)
            ).requested_revision
        except ValueError as exc:
            raise CliFailure(
                "repository_lineage.source_invalid",
                "follow-ref is not a safe Git revision selector",
            ) from exc
    heads = provider.list_remote_heads(parent.repository_url)
    chosen = next_release_line(parent.requested_revision, heads)
    if chosen is None:
        raise CliFailure(
            "repository_update.no_newer_release_line",
            "no newer release/X.Y.x line is available; already at the moving tip "
            "or no release branches were advertised",
        )
    return chosen


def _attach_update_conflict_evidence(
    args,
    root: Path,
    value: dict[str, Any],
    framework_plan,
    lineage_update,
) -> None:
    """Add display-only ours/theirs diffs, and optional plan-only reviews."""

    framework_diffs = framework_conflict_diffs(root, framework_plan)
    lineage_diffs = lineage_conflict_diffs(root, lineage_update)
    value["framework"]["conflict_diffs"] = framework_diffs
    value["repository_lineage"]["conflict_diffs"] = lineage_diffs
    value["framework"]["files"] = enrich_conflict_files(
        list(value["framework"].get("files") or []), framework_diffs
    )
    value["repository_lineage"]["files"] = enrich_conflict_files(
        list(value["repository_lineage"].get("files") or []), lineage_diffs
    )
    if not getattr(args, "review_conflicts", False):
        return
    try:
        value["conflict_reviews"] = review_update_conflicts(
            [*framework_diffs, *lineage_diffs]
        )
    except ConflictReviewError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def update_project_from_args(args) -> dict[str, Any]:
    """Plan a three-way initialized-project update; optionally record advisory items.

    The plan stays read-only. ``--record-work-items`` also appends queue entries for
    upstream deltas, which is a separate explicit act: it records decisions to make,
    never framework file changes to apply.
    """

    take_upstream = frozenset(getattr(args, "take_upstream", ()) or ())
    keep_local = frozenset(getattr(args, "keep_local", ()) or ())
    if (take_upstream or keep_local) and not getattr(args, "apply", False):
        raise CliFailure(
            "repository_update.resolution_requires_apply",
            "--take-upstream and --keep-local are explicit transaction decisions "
            "and require --apply",
        )
    try:
        project = discover_project(Path(args.path))
        if project is None:
            raise CliFailure(
                "project.not_found", f"no {PROJECT_FILENAME} found from {args.path}"
            )
        report_progress("Loading recorded parent lineage")
        selection, lineage = FilesystemRepositoryLineageStore(project.root).load()
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except RepositoryLineageStoreError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if not selection.parents:
        provider = _repository_fetch_provider(args)
        return {
            "schema": "literate-ai/project-update-noop@1",
            "mode": "no-op",
            "reason": "explicit-root-repository-has-no-parent-chain",
            "repository_parent_identity": selection.identity.uri,
            "repository_lineage_identity": lineage.identity.uri,
            "changed": False,
            "authority_review_required": False,
            "repository_fetch_deadline": provider.deadline_evidence,
        }
    provider = _repository_fetch_provider(args)
    follow_revision = _requested_follow_revision(args, selection.parents[0], provider)
    follow_selection = None
    if follow_revision != selection.parents[0].requested_revision:
        if len(selection.parents) != 1:
            raise CliFailure(
                "repository_update.follow_requires_single_parent",
                "parent follow flags require exactly one direct parent",
            )
        follow_selection = RepositoryParentSelection.inherit(
            (
                RepositoryParentReference(
                    selection.parents[0].repository_url, follow_revision
                ),
            )
        )
    follow_applied: dict[str, object] | None = None
    reparent = None
    reparent_staged = False

    def rollback_staged_reparent() -> None:
        if not reparent_staged:
            return
        assert reparent is not None and follow_plan is not None
        try:
            reparent.rollback_staged_update(Path(args.path), follow_plan)
        except RepositoryReparentError as rollback_error:
            raise CliFailure(
                "repository_update.rollback_failed",
                "composite project update failed and its staged parent selector "
                "could not be rolled back",
            ) from rollback_error

    try:
        follow_plan = None
        if follow_selection is not None:
            report_progress(f"Planning parent-selector change to {follow_revision}")
            reparent = FilesystemRepositoryReparentAdapter(
                resolver=lambda item: resolve_repository_lineage(item, provider)
            )
            follow_plan = reparent.plan(Path(args.path), follow_selection)
            if getattr(args, "apply", False):
                report_progress("Staging parent-selector change")
                follow_applied = reparent.apply(
                    Path(args.path), follow_plan, validate_authority=False
                )
                reparent_staged = True
            else:
                follow_applied = None
        report_progress("Planning inherited catalog and framework updates")
        repository_updates = FilesystemRepositoryUpdateAdapter(
            lineage_resolver=lambda item: resolve_repository_lineage(item, provider),
            catalog_planner=lambda prospective: plan_inherited_catalogs(
                prospective, provider
            ),
        )
        if follow_plan is not None and not getattr(args, "apply", False):
            lineage_update = repository_updates.plan(
                Path(args.path), reparent_plan=follow_plan
            )
        else:
            lineage_update = repository_updates.plan(Path(args.path))
        conflict_paths = frozenset(
            item.path
            for item in lineage_update.contract.files
            if item.classification is ProjectUpdateClassification.CONFLICT
        )
        invalid_take_upstream = tuple(sorted(take_upstream - conflict_paths))
        if invalid_take_upstream:
            raise RepositoryUpdateError(
                "repository_update.take_upstream_not_conflict",
                "take-upstream paths must be planned inherited-catalog conflicts: "
                + ", ".join(invalid_take_upstream),
            )
        removable_paths = frozenset(
            item.path
            for item in lineage_update.contract.files
            if item.classification is ProjectUpdateClassification.UPSTREAM_ONLY
            and item.upstream_identity is None
        )
        invalid_keep_local = tuple(sorted(keep_local - removable_paths))
        if invalid_keep_local:
            raise RepositoryUpdateError(
                "repository_update.keep_local_not_removal",
                "keep-local paths must be planned inherited-catalog removals: "
                + ", ".join(invalid_keep_local),
            )
        protected_paths = frozenset(
            item.path
            for item in lineage_update.contract.files
            if (
                (
                    item.classification
                    in {
                        ProjectUpdateClassification.LOCAL_ONLY,
                        ProjectUpdateClassification.CONFLICT,
                        ProjectUpdateClassification.PRESERVED_DYNAMIC,
                    }
                    and item.path not in take_upstream
                )
                or (
                    item.classification is ProjectUpdateClassification.UPSTREAM_ADDED
                    and not getattr(args, "adopt_added", False)
                )
                or item.path in keep_local
            )
        )
        report_progress("Classifying framework templates")
        framework_plan = ProjectUpdateService(
            FilesystemProjectUpdateAdapter(protected_paths=protected_paths)
        ).plan(Path(args.path))
    except (ProjectUpdateError, RepositoryUpdateError, RepositoryReparentError) as exc:
        rollback_staged_reparent()
        raise CliFailure(exc.code, exc.message) from exc
    except BaseException:
        rollback_staged_reparent()
        raise
    reported_lineage = lineage
    if follow_plan is not None:
        reported_lineage = follow_plan.prospective_lineage
    value = {
        "schema": "literate-ai/composite-project-update@1",
        "mode": "read-only-plan",
        "framework": framework_plan.to_dict(),
        "repository_lineage": lineage_update.contract.to_dict(),
        "parent_selectors": _parent_selector_reports(reported_lineage),
        "changed": framework_plan.previous_origin != framework_plan.upstream_origin
        or lineage_update.contract.changed
        or follow_plan is not None,
        "apply_supported": True,
        "repository_fetch_deadline": provider.deadline_evidence,
    }
    if follow_plan is not None:
        value["follow"] = {
            "schema": "literate-ai/repository-parent-follow@1",
            "from": selection.parents[0].requested_revision,
            "to": follow_revision,
            "plan": follow_plan.to_dict(),
        }
        if getattr(args, "apply", False):
            value["follow"]["applied"] = follow_applied

    if getattr(args, "apply", False):
        try:
            report_progress("Applying inherited catalog updates")

            def finalize_complete_project(root: Path) -> None:
                nonlocal framework_plan, applied, evidence_migration
                # Parent authority has precedence over distribution templates. Re-plan
                # after inherited catalogs settle, while retaining refused parent paths
                # as local authority, then validate the one complete prospective tree.
                framework_plan = ProjectUpdateService(
                    FilesystemProjectUpdateAdapter(protected_paths=protected_paths)
                ).plan(root)
                applied = apply_project_update(
                    framework_plan,
                    root,
                    adopt_added=getattr(args, "adopt_added", False),
                    validator=lambda candidate: validate_project(
                        candidate,
                        require_authority_review=False,
                        synchronize_source_intelligence=True,
                    ),
                )
                # Retained-harness evidence recorded under an older release's
                # schema is not a template file the classifier above knows
                # about; migrate it narrowly so the 0.11.0 receipt front door
                # can read qualified 0.9.0 parity and source-scope evidence.
                evidence_migration = migrate_retained_evidence(root)

            applied = None
            evidence_migration = None
            repository_applied = repository_updates.apply(
                Path(args.path),
                lineage_update,
                adopt_added=getattr(args, "adopt_added", False),
                take_upstream=take_upstream,
                keep_local=keep_local,
                finalizer=finalize_complete_project,
            )
            assert applied is not None
        except (ProjectUpdateError, RepositoryUpdateError) as exc:
            rollback_staged_reparent()
            raise CliFailure(exc.code, exc.message) from exc
        value["framework"] = framework_plan.to_dict()
        value["repository_lineage"]["applied"] = repository_applied.to_dict()
        value["framework"]["applied"] = applied.to_dict()
        if evidence_migration is not None and evidence_migration.migrated:
            value["retained_evidence_migration"] = evidence_migration.to_dict()
        value["mode"] = "applied"

    _attach_update_conflict_evidence(
        args,
        Path(args.path).resolve(),
        value,
        framework_plan,
        lineage_update,
    )

    if not getattr(args, "record_work_items", False):
        return value

    items = project_update_work_items(framework_plan)
    value["work_items"] = [item.to_dict() for item in items]
    try:
        recorded = record_work_items(
            Path(args.path).resolve(),
            items,
            queue_path=getattr(args, "queue", DEFAULT_QUEUE_PATH),
        )
    except ProjectUpdateWorkItemError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    value["work_items_recorded"] = recorded.to_dict()
    if recorded.recorded:
        # The queue is a declared document, so appending to it invalidates the review
        # marker by design. Say so here rather than let the next gate surprise anyone.
        value["work_items_recorded"]["authority_review_required"] = True
    return value


def reparent_project_from_args(args) -> dict[str, Any]:
    """Plan, and only with ``--apply`` change, complete repository ancestry."""

    raw = args.parent.strip() if isinstance(args.parent, str) else ""
    if raw.casefold() == "none":
        prospective = RepositoryParentSelection.root()
    else:
        try:
            prospective = RepositoryParentSelection.inherit(
                (repository_parent_reference(raw, base=Path.cwd()),)
            )
        except GitRepositoryLineageError as exc:
            raise CliFailure(exc.code, exc.message) from exc
    provider = _repository_fetch_provider(args)
    adapter = FilesystemRepositoryReparentAdapter(
        resolver=lambda selection: resolve_repository_lineage(selection, provider)
    )
    try:
        plan = adapter.plan(Path(args.project), prospective)
        value = plan.to_dict()
        value["repository_fetch_deadline"] = provider.deadline_evidence
        if getattr(args, "apply", False):
            value["applied"] = adapter.apply(Path(args.project), plan)
            value["mode"] = "applied"
        return value
    except RepositoryReparentError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def project_source_intelligence_from_args(args) -> dict[str, Any]:
    """Synchronize or check explicitly configured project source intelligence."""

    try:
        project = discover_project(Path(args.path))
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if project is None:
        raise CliFailure(
            "project.not_found", f"no {PROJECT_FILENAME} found from {args.path}"
        )
    try:
        validate_project_structure(project)
        policy = project.definition.source_intelligence
        if policy.provider_id == "none":
            raise CliFailure(
                "project.source_intelligence_disabled",
                "project source intelligence explicitly selects provider 'none'",
            )
        provider = CodeGraphProjectSourceIntelligence(
            policy,
            binary=getattr(args, "binary", None),
        )
        mode = policy.mode_for(SourceIntelligenceStage.PROJECT_MAINTENANCE)
        if args.source_intelligence_command == "sync":
            observation = provider.sync(project.root)
        elif args.source_intelligence_command == "check":
            observation = provider.check(project.root)
        else:
            raise CliFailure(
                "cli.usage", "a project source-intelligence command is required"
            )
        return {
            "schema": "literate-ai/source-intelligence-command-result@1",
            "status": SourceIntelligenceStageStatus(
                SourceIntelligenceStage.PROJECT_MAINTENANCE,
                mode,
                "current",
                policy.provider_id,
            ).to_dict(),
            "observation": observation,
        }
    except (ProjectError, ProjectSourceIntelligenceError) as exc:
        raise CliFailure(exc.code, exc.message) from exc


def documentation_review_from_args(args) -> dict[str, Any]:
    """Calculate or atomically record the validated current review marker."""

    try:
        selected = Path(args.path)
        if not getattr(args, "record", False):
            return FilesystemProjectValidationAdapter().documentation_review(selected)
        return record_project_authority_review(selected)
    except ProjectInitializationError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except ProjectValidationError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def documentation_update_from_args(args) -> dict[str, Any]:
    """Plan or explicitly apply bounded documentation reconciliation."""

    apply = bool(getattr(args, "apply", False))
    allow_model_egress = bool(getattr(args, "allow_model_egress", False))
    model = getattr(args, "model", None)
    if apply != allow_model_egress:
        raise CliFailure(
            "project.documentation_update_authorization_required",
            "documentation update application requires both --apply and "
            "--allow-model-egress",
        )
    if model is not None and not apply:
        raise CliFailure(
            "project.documentation_update_model_not_allowed",
            "--model is valid only with --apply and --allow-model-egress",
        )
    try:
        selected = Path(args.path)
        project = discover_project(selected)
        if project is None:
            raise CliFailure(
                "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
            )
        validator = FilesystemProjectValidationAdapter()
        validation = validator.validate(
            selected,
            require_authority_review=False,
            include_test_receipt=True,
            synchronize_source_intelligence=False,
        )
        review_value = validation.get("authority_review")
        if not isinstance(review_value, dict):
            raise DocumentationUpdateError(
                "project.documentation_update_failed",
                "project validation did not return documentation review evidence",
            )
        review = review_value
        receipt = validation.get("test_receipt", {})
        receipt_state = (
            str(receipt.get("state", "unknown"))
            if isinstance(receipt, dict)
            else "unknown"
        )
        if apply:
            from literate_ai.adapters.models import CodingCliTaskRunner

            post_review: dict[str, object] = {}

            def validate_documentation_result() -> dict[str, object]:
                post_review.update(validator.documentation_review(project.root))
                return post_review

            result = apply_documentation_update(
                project,
                review=review,
                receipt_state=receipt_state,
                authority=validation,
                task_runner=CodingCliTaskRunner(),
                model=model,
                validate_result=validate_documentation_result,
            )
            if not post_review:
                post_review.update(review)
            marker = result.get("marker")
            inventory = result.get("inventory")
            if not isinstance(marker, dict) or not isinstance(inventory, dict):
                raise DocumentationUpdateError(
                    "project.documentation_update_failed",
                    "documentation update did not return marker evidence",
                )
            marker.update(
                {
                    "after": post_review.get("state"),
                    "authority_identity": post_review.get("authority_identity"),
                    "expected_marker": post_review.get("expected_marker"),
                }
            )
            inventory.update(
                {
                    "authority_identity": post_review.get("authority_identity"),
                    "marker_state": post_review.get("state"),
                }
            )
            return result
        return plan_documentation_update(
            project, review=review, receipt_state=receipt_state
        )
    except (
        DocumentationUpdateError,
        ProjectError,
        ProjectValidationError,
        ValueError,
    ) as exc:
        if isinstance(exc, CliFailure):
            raise
        raise CliFailure(
            getattr(exc, "code", "project.documentation_update_failed"),
            getattr(exc, "message", str(exc)),
        ) from exc


def project_test_receipt_from_args(args) -> dict[str, Any]:
    """Update or verify the one configured, replaceable project test receipt."""

    if args.test_receipt_command == "verify-evidence":
        from .evidence_verification import verify_evidence_from_args

        return verify_evidence_from_args(args)
    if args.test_receipt_command == "require-current-evidence":
        from .evidence_verification import require_current_evidence_from_args

        return require_current_evidence_from_args(args)
    if args.test_receipt_command == "publish-evidence":
        from .evidence_publication import publish_evidence_from_args

        return publish_evidence_from_args(args)

    if args.test_receipt_command in {"run-retained", "update"}:
        from literate_ai.adapters.lifecycle_lock import (
            ProjectLifecycleLockError,
            project_lifecycle_lock,
        )

        try:
            project = discover_project(Path(args.project))
            if project is not None:
                with project_lifecycle_lock(
                    project.root, operation=f"test-receipt.{args.test_receipt_command}"
                ):
                    return _project_test_receipt_from_args(args)
        except (ProjectError, ProjectLifecycleLockError) as exc:
            raise CliFailure(exc.code, exc.message) from exc
    return _project_test_receipt_from_args(args)


def _project_test_receipt_from_args(args) -> dict[str, Any]:
    selected = Path(args.project)
    try:
        project_revision_identity = adapter_project_authority_identity(selected)
    except ProjectValidationError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    try:
        project = discover_project(selected)
    except ProjectError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    if project is None:
        raise CliFailure(
            "project.not_found", f"no {PROJECT_FILENAME} found from {selected}"
        )
    try:
        if args.test_receipt_command == "run-retained":
            candidate = Path(args.candidate)
            evidence = (
                Path(args.evidence)
                if args.evidence is not None
                else candidate.with_name(f"{candidate.stem}.evidence.json")
            )
            report = (
                None
                if args.known_failure_report is None
                else load_known_failure_report(Path(args.known_failure_report))
            )
            try:
                harness_workspace_links = resolve_harness_workspace_links(
                    tuple(args.harness_workspace_link),
                    base=Path.cwd(),
                    project_root=project.root,
                )
            except HarnessWorkspaceError as exc:
                raise CliFailure(exc.code, exc.message) from exc
            execution_worker, worker_catalog_identity = (
                _select_retained_execution_worker(args, project.root)
            )
            return run_retained_harness_receipt(
                project,
                candidate_path=candidate,
                evidence_path=evidence,
                worker_id=args.worker_id,
                timeout_seconds=args.timeout_seconds,
                known_failure_report=report,
                harness_workspace_links=harness_workspace_links,
                execution_worker=execution_worker,
                worker_catalog_identity=worker_catalog_identity,
            )
        if args.test_receipt_command == "update":
            return update_project_test_receipt(
                project,
                Path(args.candidate),
                project_revision_identity=project_revision_identity,
            )
        if args.test_receipt_command == "check":
            return check_project_test_receipt(
                project,
                Path(args.candidate),
                project_revision_identity=project_revision_identity,
            )
        if args.test_receipt_command == "require-current":
            try:
                return require_current_project_test_receipt(
                    project, project_revision_identity=project_revision_identity
                )
            except ProjectError as exc:
                if exc.code != "project.test_receipt_stale":
                    raise
                base_stale = exc
            try:
                locked_revision = current_rebuild_project_authority_identity(
                    project, project_revision_identity
                )
            except ProjectComponentLockSetError:
                assert base_stale is not None
                raise base_stale from None
            return require_current_project_test_receipt(
                project, project_revision_identity=locked_revision
            )
    except (
        ProjectError,
        ProjectComponentLockSetError,
        RetainedHarnessReceiptError,
    ) as exc:
        raise CliFailure(
            exc.code,
            exc.message,
            message_limit=(
                HARNESS_DIAGNOSTIC_CHARS + CLI_ERROR_MESSAGE_CHARS
                if args.test_receipt_command == "run-retained"
                and exc.code
                in {
                    "project.convert_ci_failed",
                    "project.convert_legacy_gate_failed",
                }
                else CLI_ERROR_MESSAGE_CHARS * 64
            ),
        ) from exc
    raise CliFailure("cli.usage", "a project test-receipt command is required")


def _select_retained_execution_worker(
    args: Any, project_root: Path
) -> tuple[ExecutionWorker | None, ContentIdentity | None]:
    """Resolve a non-local retained label to one exact supported SSH worker."""

    if args.worker_id == "local":
        return None, None
    try:
        configuration = resolve_worker_config_path(
            explicit=args.worker_config,
            project_root=project_root,
        )
        catalog = load_execution_worker_catalog(configuration)
        worker = catalog.worker(args.worker_id)
    except (ExecutionDispatchAdapterError, UserAssetPathError) as exc:
        raise CliFailure(exc.code, exc.message) from exc
    except ContractValidationError as exc:
        raise CliFailure(
            "retained_receipt.worker_unknown",
            f"no exact configured worker matches {args.worker_id!r}",
        ) from exc
    if worker.kind is not ExecutionWorkerKind.SSH:
        raise CliFailure(
            "retained_receipt.worker_kind_unsupported",
            "run-retained supports only local execution or a configured SSH worker",
        )
    if worker.requirements.os_family == "windows":
        raise CliFailure(
            "retained_receipt.worker_os_unsupported",
            "run-retained remote execution does not yet support Windows workers",
        )
    if worker.requirements.os_family not in {"linux", "macos"}:
        raise CliFailure(
            "retained_receipt.worker_os_unsupported",
            "run-retained requires an SSH worker with an explicit POSIX OS",
        )
    return worker, catalog.identity


def project_peer_work_from_args(args) -> dict[str, Any]:
    """Survey, mark, or safely collect peer branches and worktrees."""

    from literate_ai.adapters.peer_work import (
        PeerWorkError,
        garbage_collect_peer_work,
        mark_branch_lifecycle,
        survey_peer_work,
    )

    try:
        action = getattr(args, "peer_work_action", None)
        path = args.path
        if action not in {None, "status", "survey", "mark", "gc"}:
            if path != ".":
                raise CliFailure("cli.usage", "peer-work accepts only one path")
            path, action = action, None
        if action == "mark":
            if not args.state or not args.branch or not args.actor:
                raise CliFailure(
                    "cli.usage",
                    "peer-work mark requires --state, --branch, and --actor",
                )
            return mark_branch_lifecycle(
                Path(path),
                state=args.state,
                branch=args.branch,
                actor=args.actor,
                reason=args.reason,
                push=args.push,
            )
        if action == "gc":
            return garbage_collect_peer_work(
                Path(path),
                apply=args.apply,
                authorize_delete=args.authorize_delete,
            )
        when = getattr(args, "when", None)
        if action in {"status", "survey"}:
            when = when or "start"
        if when is None:
            raise CliFailure("cli.usage", "a peer-work action or --when is required")
        return survey_peer_work(Path(path), when=when)
    except PeerWorkError as exc:
        raise CliFailure(exc.code, exc.message) from None


def project_worktree_from_args(args) -> dict[str, Any]:
    """Report the one Git-custodied worktree placement for a checkout."""

    from literate_ai.adapters.project_worktrees import (
        ProjectWorktreeError,
        resolve_project_worktree_location,
    )

    if getattr(args, "worktree_action", None) != "location":
        raise CliFailure("cli.usage", "a project worktree action is required")
    try:
        location = resolve_project_worktree_location(Path(args.path))
    except ProjectWorktreeError as exc:
        raise CliFailure("project.worktree_location_invalid", str(exc)) from None
    return {
        **location.to_dict(),
        "location_identity": location.identity.uri,
        "placement_enforced": True,
    }


def project_tracker_from_args(args) -> dict[str, Any]:
    """Classify the issue tracker from Git remotes and name gh or glab."""

    from literate_ai.adapters.project_tracker import (
        ProjectTrackerError,
        inspect_project_tracker,
    )

    if getattr(args, "tracker_command", None) != "inspect":
        raise CliFailure("cli.usage", "a tracker command is required")
    try:
        return inspect_project_tracker(Path(args.path))
    except ProjectTrackerError as exc:
        raise CliFailure(exc.code, exc.message) from None


def project_guidance_from_args(args) -> dict[str, Any]:
    """Project configured policy into guidance for one operation."""

    from literate_ai.application.project_guidance import (
        ProjectGuidanceError,
        project_guidance,
    )

    try:
        return project_guidance(Path(args.path), operation=args.operation)
    except ProjectGuidanceError as exc:
        raise CliFailure(exc.code, exc.message) from None


def project_ci_plan_from_args(args) -> dict[str, Any]:
    """Detect test frameworks and emit a fail-closed shard/impact plan."""

    from literate_ai.ci_test_plan import CiTestPlanError, plan_ci_tests

    try:
        return plan_ci_tests(Path(args.path), mode=getattr(args, "mode", "compose"))
    except CiTestPlanError as exc:
        raise CliFailure(exc.code, exc.message) from None


def parent_checkout_from_args(args) -> dict[str, Any]:
    """Clone or update a parent repository under parents/<id>."""

    from literate_ai.adapters.parent_checkout import (
        ParentCheckoutError,
        checkout_parent,
    )

    if getattr(args, "parent_command", None) != "checkout":
        raise CliFailure("cli.usage", "a parent command is required")
    provider = _repository_fetch_provider(args)
    try:
        return checkout_parent(
            Path(args.project),
            args.source,
            deadline_policy=provider.deadline_policy,
            deadline_provenance=provider.deadline_provenance,
            deadline_field_provenance=provider.deadline_field_provenance,
        )
    except ParentCheckoutError as exc:
        raise CliFailure(exc.code, exc.message) from None


__all__ = [
    "PROJECT_INITIALIZATION_SCHEMA",
    "PROJECT_VALIDATION_SCHEMA",
    "documentation_review_from_args",
    "documentation_update_from_args",
    "init_project_from_args",
    "parent_checkout_from_args",
    "project_ci_plan_from_args",
    "project_guidance_from_args",
    "project_peer_work_from_args",
    "project_source_intelligence_from_args",
    "project_test_receipt_from_args",
    "project_tracker_from_args",
    "project_worktree_from_args",
    "validate_project_from_args",
    "update_project_from_args",
]
