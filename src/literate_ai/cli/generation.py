"""Specification-plus-Flavor source generation through a selected coding CLI."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.cache import FilesystemStandardSourceAdmissionPublisher
from literate_ai.adapters.generation_preparation import (
    FilesystemLockedGenerationApplicationAdapter,
    LockedComponentModelSelectionAdapter,
    PreparedLockedGeneration,
)
from literate_ai.adapters.legacy_generation_catalog import (
    MODEL_SELECTION_SCHEMA,
)
from literate_ai.adapters.legacy_generation_catalog import (
    load_flavor_catalog as _load_flavor_catalog,
)
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.models import (
    CODING_CLIS,
    CodingCliError,
    CodingCliSelection,
    InheritedSessionError,
    InheritedSessionProviderConfig,
    InheritedSessionSelection,
    RecipeFlavor,
    select_coding_cli,
    selected_coding_provider,
)
from literate_ai.adapters.source_verification_workspace import (
    SourceVerificationWorkspaceError,
    source_verification_workspace,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectPlanningAdapter,
    FilesystemStandardSourceGenerationAdapter,
)
from literate_ai.adapters.standard_rebuild import _resolved_source_cache
from literate_ai.application import (
    GenerationExecutionPlan,
    StandardSourceAdmissionRequest,
    StandardSourceAdmissionService,
    StandardSourceVerification,
)
from literate_ai.application.generation_preparation import (
    GenerationPreparationError,
    GenerationPreparationRequest,
)
from literate_ai.cache_directories import resolve_cache_directories
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    SourceIntelligenceStage,
    StandardProjectLifecycleDriver,
    StandardSourceSelector,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    StandardSourceTestResult,
    canonical_identity,
)
from literate_ai.generated_tests import GENERATED_TEST_SUITE_PATH
from literate_ai.project_source_index import (
    ProjectSourceIntelligenceError,
    require_lifecycle_project_index,
)
from literate_ai.projects import (
    PinnedInputClosure,
    PinnedInputClosureError,
    discover_project,
)
from literate_ai.storage import FileSystemCAS

from .errors import CliFailure

GENERATION_RESULT_SCHEMA = "literate-ai/coding-cli-generation@11"
LEGACY_GENERATION_RESULT_SCHEMA = "literate-ai/coding-cli-generation@10"
OLDER_GENERATION_RESULT_SCHEMA = "literate-ai/coding-cli-generation@9"
GENERATION_PLAN_SCHEMA = "literate-ai/generation-plan@6"


def load_flavor_catalog(roots: Sequence[Path]) -> tuple[RecipeFlavor, ...]:
    """CLI-compatible presentation wrapper around the filesystem adapter."""

    try:
        return _load_flavor_catalog(roots)
    except GenerationPreparationError as exc:
        raise CliFailure(exc.code, str(exc)) from exc


_LOCKED_GENERATION = FilesystemLockedGenerationApplicationAdapter()


def _require_generation_project_index(project, *, synchronize: bool) -> None:
    if project is None:
        return
    try:
        require_lifecycle_project_index(
            project.root,
            project.definition.source_intelligence,
            stage=SourceIntelligenceStage.SOURCE_GENERATION,
            synchronize=synchronize,
        )
    except ProjectSourceIntelligenceError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def _prepare_generation(args) -> PreparedLockedGeneration:
    component_root = Path(args.specification)
    explicit_selectors = tuple(getattr(args, "flavor", ()))
    project = discover_project(component_root)
    selectors = (
        explicit_selectors
        if project is None
        else project.flavor_selectors_for(component_root, explicit_selectors)
    )
    request = GenerationPreparationRequest(
        component_root=component_root,
        target_name=getattr(args, "target", "host"),
        flavor_selectors=selectors,
        flavor_roots=tuple(Path(item) for item in getattr(args, "flavor_root", ())),
        recipe_id=args.recipe_id,
    )
    try:
        return _LOCKED_GENERATION.prepare(request)
    except (
        GenerationPreparationError,
        PinnedInputClosureError,
        LockedGenerationAuthorityReaderError,
    ) as exc:
        message = getattr(exc, "message", str(exc))
        raise CliFailure(exc.code, message) from exc


def _execution_plan(
    prepared: PreparedLockedGeneration,
    *,
    selection: CodingCliSelection | None = None,
    model: str | None = None,
) -> GenerationExecutionPlan:
    try:
        return _LOCKED_GENERATION.plan(
            prepared,
            selection=selection,
            model=model,
        )
    except (
        GenerationPreparationError,
        PinnedInputClosureError,
        LockedGenerationAuthorityReaderError,
    ) as exc:
        raise CliFailure(exc.code, getattr(exc, "message", str(exc))) from exc


def _require_generation_inputs_unchanged(
    prepared: PreparedLockedGeneration,
) -> PinnedInputClosure:
    try:
        return _LOCKED_GENERATION.require_unchanged(prepared)
    except (GenerationPreparationError, PinnedInputClosureError) as exc:
        raise CliFailure(exc.code, getattr(exc, "message", str(exc))) from exc
    except LockedGenerationAuthorityReaderError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def _input_closure_report(closure: PinnedInputClosure) -> dict[str, object]:
    return {
        "identity": closure.identity,
        "file_count": closure.file_count,
        "total_bytes": closure.total_bytes,
    }


def plan_from_args(args) -> dict[str, Any]:
    """Resolve and verify every input without invoking a coding CLI."""
    from .repository_locks import repository_plan_from_args, selected_repository_root

    root = selected_repository_root(args, Path(args.specification))
    if root is not None:
        return repository_plan_from_args(args, root)

    from literate_ai.diagnostics import debug_stage

    with debug_stage("plan", specification=str(getattr(args, "specification", ""))):
        return _plan_from_args_body(args)


def _plan_from_args_body(args) -> dict[str, Any]:
    """Resolve and verify every input without invoking a coding CLI."""

    prepared = _prepare_generation(args)
    recipe = prepared.recipe
    execution = _execution_plan(prepared)
    project = discover_project(prepared.component_root)
    _require_generation_project_index(project, synchronize=False)
    input_closure = _require_generation_inputs_unchanged(prepared)
    locked_authority = prepared.locked_authority_snapshot
    component_lock = locked_authority.authority.lock
    pipeline_model = getattr(args, "model", None)
    try:
        provider_kind = selected_coding_provider()
        inherited_selection = (
            InheritedSessionSelection(
                InheritedSessionProviderConfig.from_environment().provider_identity
            )
            if provider_kind == "inherited-session"
            else None
        )
    except InheritedSessionError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    planner = FilesystemStandardProjectPlanningAdapter(
        coding_cli_selector=(
            (lambda: inherited_selection)
            if inherited_selection is not None
            else select_coding_cli
        ),
        model_selector=LockedComponentModelSelectionAdapter(
            pipeline_model=pipeline_model
        ),
    )
    try:
        standard = planner.plan(locked_authority)
    except CodingCliError as exc:
        if exc.code != "coding_cli.unavailable":
            raise
        logical_coding_cli = os.environ.get("CODING_CLI", "").strip() or CODING_CLIS[0]
        if logical_coding_cli not in CODING_CLIS:
            raise
        standard_execution = planner.plan_for_coding_cli(
            locked_authority,
            coding_cli=logical_coding_cli,
        )
        selected_provider = logical_coding_cli
        selected_coding_cli: dict[str, object] = {
            "coding_cli": logical_coding_cli,
            "binding_state": "unbound-unavailable-at-plan-time",
        }
    else:
        standard_execution = standard.execution_plan
        selected_provider = standard.coding_cli.name
        selected_coding_cli = {
            **standard.coding_cli.to_dict(),
            "selection_identity": standard.coding_cli.identity,
            "tool_binding_identity": standard.coding_cli.tool_binding_identity,
            "binding_state": "exact-host-binding",
        }
    return {
        "schema": GENERATION_PLAN_SCHEMA,
        "project": (
            {
                "project_id": project.definition.project_id,
                "identity": project.definition.identity.uri,
                "default_flavor_selectors": list(
                    project.definition.default_flavor_selectors
                ),
            }
            if project is not None
            else None
        ),
        "component": {
            "coordinate": prepared.definition.coordinate.uri,
            "version": prepared.definition.version,
            "identity": (locked_authority.authority.root_authoring.identity.uri),
            "revision_identity": (
                prepared.component_revision.identity.uri
                if prepared.component_revision is not None
                else None
            ),
        },
        "resolution": {
            "component_lock_identity": (component_lock.identity.uri),
            "resolved_graph_identity": (component_lock.identity.uri),
            "selection_policy_identity": (component_lock.selection_policy_identity.uri),
            "target_profile_identity": (component_lock.target_profile_identity.uri),
            "root_revision_identity": (component_lock.root_revision.uri),
            "provider_resolutions": [
                item.to_dict() for item in component_lock.provider_resolutions
            ],
        },
        "recipe_identity": recipe.identity,
        "standard_component_execution": standard_execution.to_dict(),
        "flavor_catalog_audit_identity": (
            prepared.flavor_catalog_audit_identity.uri
            if prepared.flavor_catalog_audit_identity is not None
            else None
        ),
        "input_closure": _input_closure_report(input_closure),
        "specifications": [
            {"path": item.path, "identity": item.identity} for item in recipe.documents
        ],
        "selected_flavors": [
            {
                "id": item.flavor_id,
                "coordinate": item.coordinate_uri,
                "axis": item.axis,
                "value": item.value,
                "revision_identity": item.revision_identity,
                **({"slot_ids": list(item.slot_ids)} if item.slot_ids else {}),
            }
            for item in prepared.selected_flavors
        ],
        "flavor_bindings": [
            {
                "slot_id": slot_id,
                "flavor_id": item.flavor_id,
                "revision_identity": item.revision_identity,
            }
            for item in prepared.selected_flavors
            for slot_id in item.slot_ids
        ],
        "toolchain_constraints": [
            item.to_dict() for item in prepared.toolchain_constraints
        ],
        "generation_skills": [item.to_dict() for item in recipe.resolved_skills],
        "model_scopes": [
            {
                "component_revision": component_revision,
                "binding": binding.to_dict(),
            }
            for component_revision, binding in sorted(
                planner.model_selector.bindings(
                    locked_authority, coding_cli=selected_provider
                ).items()
            )
        ],
        "workflow": {
            "reference": execution.workflow_reference.to_dict(),
            "model_stages": [
                {
                    "stage_id": item.stage_id,
                    "dependencies": list(item.dependencies),
                    "produces_tree": item.produces_tree,
                    "content_kind": item.content_kind,
                }
                for item in execution.model_stages
            ],
            "lifecycle_steps": list(execution.lifecycle_steps),
        },
        "routing": {
            "reference": execution.routing_reference.to_dict(),
            "decision_timing": "generation-after-coding-cli-selection",
        },
        "required_entrypoint": recipe.required_entrypoint,
        "required_entrypoints": list(recipe.all_required_entrypoints),
        "coding_cli_selection": {
            "environment": "CODING_CLI",
            "path_order": list(CODING_CLIS),
            "selected": (
                {
                    **selected_coding_cli,
                }
            ),
        },
    }


def _require_reviewed_component_authority(
    prepared: PreparedLockedGeneration,
) -> ContentIdentity | None:
    from literate_ai.adapters.project_validation import (
        ProjectValidationError,
        validated_project_authority_identity,
    )

    def review(root: Path) -> ContentIdentity:
        try:
            return validated_project_authority_identity(
                root, synchronize_source_intelligence=False
            )
        except ProjectValidationError as exc:
            raise CliFailure(exc.code, exc.message) from exc

    try:
        return _LOCKED_GENERATION.review(prepared, review)
    except (
        GenerationPreparationError,
        PinnedInputClosureError,
        LockedGenerationAuthorityReaderError,
    ) as exc:
        raise CliFailure(exc.code, getattr(exc, "message", str(exc))) from exc


def _require_disposable_output(output: Path, boundary: Path, build_dir: Path) -> None:
    """Keep generated source out of authority, but allow the project's build directory.

    Disposable source must never sit among specifications, so output inside the project
    root is refused. The build directory is the exception, and has to be: `litai init`
    creates it, `.gitignore` covers it, and `rebuild` and `build` already write there.
    Refusing it too made the framework contradict its own scaffolding -- the directory
    named for generated output was the one place `generate` would not write.
    """

    resolved = output.resolve()
    inside_project = resolved == boundary or resolved.is_relative_to(boundary)
    if not inside_project:
        return
    build_root = build_dir.resolve()
    if resolved == build_root or resolved.is_relative_to(build_root):
        return
    raise CliFailure(
        "generate.output_overlaps_specification",
        "generated source output must not sit among specifications; write it beneath "
        f"the project build directory ({build_root}) or outside the project root",
    )


_VERIFIER_FAILURE_DETAIL_BYTES = 2000


def _verifier_failure_detail(completed: subprocess.CompletedProcess) -> str:
    """Summarize a failed verifier command without dumping its complete transcript."""

    for label, stream in (("stderr", completed.stderr), ("stdout", completed.stdout)):
        collapsed = " ".join((stream or b"").decode("utf-8", errors="replace").split())
        if collapsed:
            return f"last {label}: {collapsed[-_VERIFIER_FAILURE_DETAIL_BYTES:]}"
    return "the command produced no output"


@dataclass(frozen=True, slots=True)
class _CommandSourceVerifier:
    source_root: Path
    candidate_manifest_identity: ContentIdentity
    commands: tuple[tuple[str, ...], ...]

    def verify(self, generation) -> StandardSourceVerification:
        try:
            with source_verification_workspace(
                self.source_root, generation.output.candidate.tree_identity
            ) as workspace:
                return self._verify_in_workspace(generation, workspace)
        except SourceVerificationWorkspaceError as exc:
            raise CliFailure(
                "source_admission.source_changed",
                "source candidate or verification copy is unavailable, unsafe, "
                "over its capture limits, or changed",
            ) from exc

    def _verify_in_workspace(self, generation, workspace) -> StandardSourceVerification:
        candidate = generation.output.candidate
        component_revision = candidate.component_revision.uri
        if candidate.source_manifest_identity != self.candidate_manifest_identity:
            raise CliFailure(
                "source_admission.source_manifest_mismatch",
                "generated source candidate changed before verification",
            )
        suite_path = workspace.root.joinpath(*GENERATED_TEST_SUITE_PATH.split("/"))
        try:
            suite = json.loads(suite_path.read_text(encoding="utf-8"))
            cases = suite["cases"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise CliFailure(
                "source_admission.test_evidence_missing",
                "generated source has no readable canonical test suite",
            ) from exc
        if not isinstance(cases, list) or not cases:
            raise CliFailure(
                "source_admission.test_evidence_missing",
                "generated source canonical test suite is empty",
            )
        results = []
        for command in self.commands:
            workspace.require_unchanged()
            try:
                completed = run_with_tree_kill(
                    command,
                    cwd=workspace.root,
                    timeout=900,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CliFailure(
                    "source_admission.tests_failed",
                    f"{component_revision}: source verifier command failed to "
                    f"execute: {command[0]}",
                ) from exc
            workspace.require_unchanged()
            if (
                completed.returncode != 0
                or len(completed.stdout) > 1024 * 1024
                or len(completed.stderr) > 1024 * 1024
            ):
                raise CliFailure(
                    "source_admission.tests_failed",
                    f"{component_revision}: source verifier command "
                    f"{command[0]!r} exited {completed.returncode}; "
                    + _verifier_failure_detail(completed),
                )
            result_identity = canonical_identity(
                {
                    "schema": "literate-ai/source-verifier-command-result@1",
                    "argv": list(command),
                    "exit_status": completed.returncode,
                    "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
                }
            )
            oracle_identity = canonical_identity(
                {
                    "schema": "literate-ai/source-verifier-command@1",
                    "argv": list(command),
                }
            )
            results.append(
                StandardSourceTestResult(
                    oracle_identity,
                    result_identity,
                    len(cases),
                    len(cases),
                    0,
                    0,
                )
            )
        results.sort(key=lambda item: item.identity.uri)
        plan = canonical_identity(
            {
                "schema": "literate-ai/source-verifier-test-plan@1",
                "commands": [list(command) for command in self.commands],
                "generated_test_suite_identity": (
                    candidate.generated_test_suite_identity.uri
                ),
            }
        )
        verifier = canonical_identity(
            {
                "schema": "literate-ai/command-source-verifier@1",
                "implementation": "literate_ai.cli.generation._CommandSourceVerifier",
                "test_plan_identity": plan.uri,
            }
        )
        return StandardSourceVerification(
            candidate.source_manifest_identity,
            plan,
            tuple(results),
            verifier,
        )


def _source_test_commands(args) -> tuple[tuple[str, ...], ...]:
    commands = []
    for raw in getattr(args, "source_test_command", ()):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CliFailure(
                "source_admission.test_command_invalid",
                "--source-test-command must be a JSON argv array",
            ) from exc
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise CliFailure(
                "source_admission.test_command_invalid",
                "--source-test-command must be a non-empty JSON string array",
            )
        commands.append(tuple(value))
    if not commands:
        raise CliFailure(
            "source_admission.test_command_missing",
            "--admit requires at least one verifier-owned --source-test-command",
        )
    if len(set(commands)) != len(commands):
        raise CliFailure(
            "source_admission.test_command_duplicate",
            "--source-test-command values must be unique",
        )
    return tuple(commands)


def _source_selectors(args) -> StandardSourceSelectorSet:
    if getattr(args, "source_portability", "target-specific") == "target-independent":
        return StandardSourceSelectorSet(
            StandardSourceSelectorScope.TARGET_INDEPENDENT, ()
        )
    return StandardSourceSelectorSet(
        StandardSourceSelectorScope.TARGET_SPECIFIC,
        (StandardSourceSelector("target.profile", args.target),),
    )


def _admit_generated_source(args, generated, project, directories):
    commands = _source_test_commands(args)
    driver = project.definition.lifecycle_driver
    if not isinstance(driver, StandardProjectLifecycleDriver):
        raise CliFailure(
            "source_admission.standard_driver_required",
            "source admission requires the project-pinned Standard lifecycle",
        )
    binding = resolve_standard_project_lifecycle_driver(driver)
    binding.require_unchanged()
    candidate_cas = FileSystemCAS(directories.build_dir / "candidate-cas")
    accepted_cas = FileSystemCAS(directories.build_dir / "accepted-source-cas")
    configuration, caches = _resolved_source_cache(
        project, directories.build_dir / "accepted-source-cache"
    )
    from literate_ai.adapters.cache import SourceCacheResolver

    resolver = SourceCacheResolver(configuration, caches)
    by_tree = {
        item.generation.output.candidate.tree_identity: item
        for item in generated.admission_candidates
    }
    admitted = []
    for item in generated.admission_candidates:
        candidate = item.generation.output.candidate
        manifest_path = (
            candidate_cas.blob_root
            / candidate.source_manifest_identity.digest[:2]
            / candidate.source_manifest_identity.digest
        )
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            != candidate.source_manifest_identity.digest
        ):
            raise CliFailure(
                "source_admission.source_manifest_mismatch",
                "generated source manifest is absent or changed in candidate CAS",
            )
        accepted_manifest = accepted_cas.put_file(
            manifest_path,
            media_type="application/vnd.literate-ai.generated-source-manifest+json",
        )
        if accepted_manifest.identity != candidate.source_manifest_identity.uri:
            raise CliFailure(
                "source_admission.source_manifest_mismatch",
                "accepted CAS stored another generated source manifest identity",
            )
        publisher = FilesystemStandardSourceAdmissionPublisher(
            cache=resolver,
            caller_cas=accepted_cas,
            source_root=lambda identity, values=by_tree: values[identity].source_root,
            source_custody=lambda identity, values=by_tree: type(
                "SourceCustody",
                (),
                {
                    "source_bom_content": values[identity]
                    .source_root.joinpath(*CYCLONEDX_SOURCE_SBOM_PATH.split("/"))
                    .read_bytes(),
                    "generated_test_suite_content": values[identity]
                    .source_root.joinpath(*GENERATED_TEST_SUITE_PATH.split("/"))
                    .read_bytes(),
                },
            )(),
            cache_key=lambda _membership, key=item.cache_key: key,
        )
        membership = StandardSourceAdmissionService(
            _CommandSourceVerifier(
                item.source_root,
                candidate.source_manifest_identity,
                commands,
            ),
            publisher,
        ).admit(
            StandardSourceAdmissionRequest(
                item.generation,
                item.cache_key,
                item.flavor_set_identity,
                item.skill_closure_identity,
                item.cache_key.coding_cli_tool_binding_identity,
                item.coding_cli_transcript_identity,
                _source_selectors(args),
                binding.distribution.identity,
                item.inherited_session_handoff,
            )
        )
        admitted.append(
            {
                "component_revision": candidate.component_revision.uri,
                "membership_identity": membership.identity.uri,
                "evidence_identity": membership.evidence.identity.uri,
                "source_tree_identity": candidate.tree_identity.uri,
            }
        )
    binding.require_unchanged()
    return admitted


def generate_from_args(args) -> dict[str, Any]:
    from literate_ai.diagnostics import debug_enabled, debug_stage
    from literate_ai.spec_map import instrument_generated_tree

    with debug_stage("generate", specification=str(getattr(args, "specification", ""))):
        result = _generate_from_args_body(args)
        if debug_enabled():
            output = Path(args.output)
            source_root = output / "source" if (output / "source").is_dir() else output
            result["spec_map"] = instrument_generated_tree(source_root)
        return result


def _generate_from_args_body(args) -> dict[str, Any]:
    prepared = _prepare_generation(args)
    project_authority_identity = _require_reviewed_component_authority(prepared)
    component_lock = prepared.locked_authority_snapshot.authority.lock
    output = Path(args.output)
    project = discover_project(prepared.component_root)
    _require_generation_project_index(project, synchronize=True)
    authority_root = project.root if project is not None else prepared.boundary
    directories = resolve_cache_directories(authority_root)
    _require_disposable_output(output, prepared.boundary, directories.build_dir)
    input_closure = _require_generation_inputs_unchanged(prepared)
    try:
        adapter = FilesystemStandardSourceGenerationAdapter.from_environment(
            project_root=authority_root,
            # Match rebuild: the cache lives in its own subdirectory, not at
            # BUILD_DIR itself. Adopting BUILD_DIR as the cache root made any
            # other content there -- including generated output -- look like
            # an unmarked foreign cache.
            cache_root=directories.build_dir / "generated-source-cache",
            cas_root=directories.build_dir / "candidate-cas",
            pipeline_model=getattr(args, "model", None),
            project_authority_identity=project_authority_identity,
        )
    except (CodingCliError, InheritedSessionError) as exc:
        raise CliFailure(exc.code, exc.message) from exc
    _require_generation_inputs_unchanged(prepared)
    try:
        generated = adapter.generate(
            prepared.locked_authority_snapshot,
            output_root=output,
        )
    except InheritedSessionError as exc:
        raise CliFailure(exc.code, exc.message) from exc
    selection = generated.coding_cli
    coding_cli = selection.name
    failures = {
        item.component_revision.uri: item.result.failure_code
        for item in generated.custody.components
        if item.result.failure_code is not None
    }
    if failures:
        codes = set(failures.values())
        if "coding_cli.authentication_required" in codes:
            raise CliFailure(
                "coding_cli.authentication_required",
                f"{coding_cli} requires an authenticated interactive session or "
                "environment credential before source generation",
            )
        raise CliFailure(
            "generate.source_generation_failed",
            "one or more Component source generations failed: "
            + ", ".join(
                f"{revision}={code}" for revision, code in sorted(failures.items())
            ),
        )
    result = {
        "schema": GENERATION_RESULT_SCHEMA,
        "project_authority_identity": (
            None
            if project_authority_identity is None
            else project_authority_identity.uri
        ),
        "component_lock_identity": component_lock.identity.uri,
        "resolved_graph_identity": component_lock.identity.uri,
        "coding_cli": coding_cli,
        "coding_provider": selected_coding_provider(),
        "coding_cli_executable": selection.executable,
        "coding_cli_executable_identity": selection.executable_identity,
        "coding_cli_selection_identity": selection.identity,
        "coding_cli_tool_binding_identity": selection.tool_binding_identity,
        "input_closure": _input_closure_report(input_closure),
        "standard_source_generation": generated.custody.to_dict(),
        "standard_source_generation_identity": generated.custody.identity.uri,
        "local_generated_source_cache": generated.local_cache_report,
    }
    if getattr(args, "admit", False):
        if project is None:
            raise CliFailure(
                "source_admission.project_required",
                "source admission requires a discovered Literate AI project",
            )
        result["source_admissions"] = _admit_generated_source(
            args, generated, project, directories
        )
    return result


__all__ = [
    "GENERATION_PLAN_SCHEMA",
    "GENERATION_RESULT_SCHEMA",
    "LEGACY_GENERATION_RESULT_SCHEMA",
    "OLDER_GENERATION_RESULT_SCHEMA",
    "MODEL_SELECTION_SCHEMA",
    "generate_from_args",
    "load_flavor_catalog",
    "plan_from_args",
]
