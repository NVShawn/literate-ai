"""Outer-finalized project receipts for qualified retained legacy harnesses."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai.adapters.component_lock_application import (
    ProjectComponentLockSetError,
    current_component_lock_identity,
    current_project_component_lock_identities,
)
from literate_ai.adapters.harness_inventory import (
    HARNESS_INVENTORY_SCHEMA,
    HARNESS_PARITY_SCHEMA,
    HarnessBaselineError,
    copy_retained_source_tree,
    execute_retained_harness,
    stages_covered_by_root,
    validate_harness_command_timeout,
)
from literate_ai.adapters.harness_tree import observe_retained_tree
from literate_ai.adapters.harness_workspace import (
    HarnessWorkspaceError,
    HarnessWorkspaceLink,
    harness_workspace_runtime_identity,
    materialize_harness_workspace_links,
    require_harness_workspace_link_evidence,
    validate_harness_workspace_links,
)
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
    validated_project_authority_identity,
)
from literate_ai.adapters.retained_harness_remote import (
    RetainedHarnessRemoteError,
    RetainedHarnessSshExecutor,
)
from literate_ai.contracts import (
    ContentIdentity,
    ExecutionWorker,
    KnownTestFailureReport,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
    canonical_json_bytes,
    rebuild_project_authority_identity,
)
from literate_ai.projects import LoadedProject, ProjectError
from literate_ai.test_receipts import (
    write_project_test_receipt_finalized_candidate,
)

RETAINED_HARNESS_RECEIPT_SCHEMA = "literate-ai/retained-harness-receipt-run@1"
RETAINED_HARNESS_RUNNER_SCHEMA = "literate-ai/retained-harness-runner@1"
RETAINED_HARNESS_SUITE_ID = "retained-legacy-parity"
RETAINED_HARNESS_SUITE_VERSION = "1.0.0"
_INVENTORY_PATH = ".literate/harness-inventory.json"
_BASELINE_PATH = ".literate/legacy-harness-baseline.json"
_PARITY_PATH = ".literate/legacy-wrapper-parity.json"
_LIFT_SHIFT_PATH = ".literate/legacy-lift-shift.json"
_WRAPPER_PATH = "litai.harness.mk"
_WRAPPER_COMPONENT_PATH = "components/legacy-project-wrapper"
_MAX_EVIDENCE_DOCUMENT_BYTES = 16 * 1024 * 1024
_BASE_EVIDENCE_KINDS = frozenset(
    {
        "lifecycle-command",
        "lifecycle-plan",
        "lifecycle-request",
        "observation-result",
        "source-cache-decision",
        "source-cache-lifecycle",
        "test-report",
        "test-runner",
        "workspace-admission",
    }
)


class RetainedHarnessReceiptError(RuntimeError):
    """Retained execution cannot honestly cross the passing-receipt boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def retained_harness_runner_identity(
    inventory: dict[str, object],
) -> ContentIdentity:
    """Identity of the framework-owned retained harness connector protocol."""

    return canonical_identity(
        {
            "schema": RETAINED_HARNESS_RUNNER_SCHEMA,
            "suite_id": RETAINED_HARNESS_SUITE_ID,
            "suite_version": RETAINED_HARNESS_SUITE_VERSION,
            "execution": "inventory-admitted-disposable-source-copy",
            "receipt_boundary": "supported-api-tcb",
            "inventory_identity": canonical_identity(inventory).uri,
        }
    )


def retained_harness_receipt_policy(
    inventory: dict[str, object],
) -> ProjectTestReceiptPolicy:
    """Derive the converted project's retained-only receipt policy."""

    phase_kinds = _inventory_phase_kinds(inventory)
    evidence = set(_BASE_EVIDENCE_KINDS)
    if "build" in phase_kinds:
        evidence.add("build-result")
    if "package" in phase_kinds:
        evidence.add("package-result")
    return ProjectTestReceiptPolicy(
        suite_id=RETAINED_HARNESS_SUITE_ID,
        suite_version=RETAINED_HARNESS_SUITE_VERSION,
        runner_identity=retained_harness_runner_identity(inventory),
        required_evidence_kinds=tuple(sorted(evidence)),
        minimum_test_count=1,
    )


def retained_harness_worker_identity(
    worker_id: str, execution_worker: ExecutionWorker | None = None
) -> ContentIdentity:
    """Bind local execution to its platform or remote execution to its catalog entry."""

    if (
        not worker_id
        or len(worker_id) > 256
        or any(character.isspace() for character in worker_id)
    ):
        _error(
            "retained_receipt.worker_invalid",
            "worker ID must be one bounded non-whitespace identifier",
        )
    if execution_worker is None:
        return canonical_identity(
            {
                "schema": "literate-ai/retained-harness-worker@1",
                "worker_id": worker_id,
                "platform": _platform_observation(),
            }
        )
    if execution_worker.worker_id != worker_id:
        _error(
            "retained_receipt.worker_identity_mismatch",
            "selected execution worker does not match the requested worker ID",
        )
    return canonical_identity(
        {
            "schema": "literate-ai/retained-harness-worker@2",
            "worker_id": worker_id,
            "execution_worker_identity": execution_worker.identity.uri,
        }
    )


def retained_harness_project_authority_identity(
    project: LoadedProject,
    validated_project_authority_identity: ContentIdentity,
) -> ContentIdentity:
    """Bind retained evidence documents and current authored source into authority."""

    if not isinstance(project, LoadedProject):
        raise TypeError("project must be a LoadedProject")
    if not isinstance(validated_project_authority_identity, ContentIdentity):
        raise TypeError(
            "validated_project_authority_identity must be a ContentIdentity"
        )
    identity, _source_tree = _retained_project_authority_observation(
        project,
        validated_project_authority_identity,
    )
    return identity


def run_retained_harness_receipt(
    project: LoadedProject,
    *,
    candidate_path: Path,
    evidence_path: Path,
    worker_id: str,
    timeout_seconds: int,
    known_failure_report: KnownTestFailureReport | None = None,
    harness_workspace_links: tuple[HarnessWorkspaceLink, ...] = (),
    execution_worker: ExecutionWorker | None = None,
    worker_catalog_identity: ContentIdentity | None = None,
) -> dict[str, object]:
    """Run exact admitted commands and atomically expose a finalized candidate."""

    if not isinstance(project, LoadedProject):
        raise TypeError("project must be a LoadedProject")
    if execution_worker is None and worker_id != "local":
        _error(
            "retained_receipt.worker_selection_invalid",
            "a non-local worker ID requires one exact configured execution worker",
        )
    if (execution_worker is None) != (worker_catalog_identity is None):
        _error(
            "retained_receipt.worker_selection_invalid",
            "remote retained execution requires one exact worker catalog binding",
        )
    try:
        timeout_seconds = validate_harness_command_timeout(timeout_seconds)
    except ValueError as exc:
        _error(
            "retained_receipt.timeout_invalid",
            str(exc),
        )
    candidate = _external_new_path(project, candidate_path, "candidate")
    evidence_destination = _external_new_path(project, evidence_path, "evidence")
    if candidate == evidence_destination:
        _error(
            "retained_receipt.path_collision",
            "candidate and evidence paths must be different external files",
        )

    documents = _load_retained_documents(project)
    inventory = documents["inventory"]["value"]
    assert isinstance(inventory, dict)
    try:
        validate_harness_workspace_links(
            harness_workspace_links, project_root=project.root
        )
        require_harness_workspace_link_evidence(
            inventory.get("workspace_links"), harness_workspace_links
        )
    except HarnessWorkspaceError as exc:
        _error(exc.code, exc.message)
    expected_policy = retained_harness_receipt_policy(inventory)
    if project.definition.test_receipt_policy != expected_policy:
        _error(
            "retained_receipt.policy_mismatch",
            "project does not authorize the exact retained harness runner and suite",
        )
    _require_qualified_parity(documents)
    implementation = _implementation_root(project, documents)

    try:
        validated_revision = validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        component_locks = current_project_component_lock_identities(project)
        wrapper_lock_identity = _current_wrapper_lock_identity(project)
        if wrapper_lock_identity not in component_locks:
            _error(
                "retained_receipt.wrapper_lock_changed",
                "retained wrapper lock changed while project locks were inspected",
            )
    except (ProjectValidationError, ProjectComponentLockSetError) as exc:
        _error(
            getattr(exc, "code", "retained_receipt.project_invalid"),
            getattr(exc, "message", str(exc)),
        )
    base_revision, retained_source_tree = _retained_project_authority_observation(
        project,
        validated_revision,
        documents=documents,
        implementation=implementation,
    )
    project_revision = rebuild_project_authority_identity(
        base_revision, component_locks
    )
    if execution_worker is not None and harness_workspace_links:
        _error(
            "retained_receipt.remote_workspace_links_unsupported",
            "remote retained execution does not yet transport external workspace links",
        )
    worker_identity = retained_harness_worker_identity(worker_id, execution_worker)
    stage_plan = _stage_plan(inventory)
    lifecycle_plan_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-harness-plan@1",
            "inventory_identity": documents["inventory"]["identity"],
            "stages": stage_plan,
        }
    )
    workspace_runtime_identity = harness_workspace_runtime_identity(
        harness_workspace_links
    )
    lifecycle_request_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-harness-request@1",
            "project_id": project.definition.project_id,
            "project_revision_identity": project_revision.uri,
            "runner_identity": retained_harness_runner_identity(inventory).uri,
            "worker_identity": worker_identity.uri,
            "lifecycle_plan_identity": lifecycle_plan_identity.uri,
            "timeout_seconds": timeout_seconds,
            "workspace_runtime_identity": workspace_runtime_identity.uri,
        }
    )
    lifecycle_command_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-harness-command-set@1",
            "request_identity": lifecycle_request_identity.uri,
            "commands": stage_plan,
        }
    )
    source_cache_control_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-harness-cache-control@1",
            "request_identity": lifecycle_request_identity.uri,
            "disposition": "not-applicable-disposable-source-copy",
        }
    )
    source_cache_decision_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-harness-cache-decision@1",
            "control_identity": source_cache_control_identity.uri,
            "decision": "not-applicable",
        }
    )
    source_cache_lifecycle_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-harness-cache-lifecycle@1",
            "control_identity": source_cache_control_identity.uri,
            "decision_identity": source_cache_decision_identity.uri,
            "state": "not-used",
        }
    )

    source_scope = inventory.get("source_scope")
    try:
        with tempfile.TemporaryDirectory(
            prefix="literate-ai-retained-receipt-"
        ) as temporary_directory:
            workspace_root = Path(temporary_directory)
            execution_root = workspace_root / "implementation"
            copy_retained_source_tree(implementation, execution_root, source_scope)
            if execution_worker is None:
                platform_observation = _platform_observation()
                with materialize_harness_workspace_links(
                    workspace_root, harness_workspace_links
                ):
                    report = execute_retained_harness(
                        inventory,
                        legacy_root=execution_root,
                        timeout_seconds=timeout_seconds,
                    )
                phases = _sanitized_phases(report)
            else:
                assert worker_catalog_identity is not None
                remote = RetainedHarnessSshExecutor().execute(
                    execution_worker,
                    project_id=project.definition.project_id,
                    project_revision_identity=project_revision,
                    worker_catalog_identity=worker_catalog_identity,
                    runner_identity=retained_harness_runner_identity(inventory),
                    lifecycle_request_identity=lifecycle_request_identity,
                    inventory=inventory,
                    inventory_bytes=documents["inventory"]["bytes"],
                    source_root=execution_root,
                    source_identity=ContentIdentity.parse_uri(
                        str(retained_source_tree["identity"])
                    ),
                    timeout_seconds=timeout_seconds,
                    cwd=project.root,
                )
                platform_observation = dict(remote.platform)
                phases = [dict(item) for item in remote.phases]
    except RetainedHarnessRemoteError as exc:
        _error(exc.code, exc.message)
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
            _error(
                "project.host_storage_exhausted",
                "A local retained-harness filesystem operation reported exhausted "
                "space or quota. Check workspace, temp and cache free space, "
                "quotas and inodes before retrying.",
            )
        _error("retained_receipt.execution_failed", str(exc))
    except (
        HarnessBaselineError,
        HarnessWorkspaceError,
        ValueError,
    ) as exc:
        _error(
            getattr(exc, "code", "retained_receipt.execution_failed"),
            getattr(exc, "message", str(exc)),
        )
    tests = _strict_test_total(
        phases,
        project_revision=project_revision,
        worker_identity=worker_identity,
        known_failure_report=known_failure_report,
    )
    _require_project_unchanged(project, base_revision, component_locks)

    admission_identity = _workspace_admission_identity(documents)
    test_report_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-harness-test-report@1",
            "request_identity": lifecycle_request_identity.uri,
            "tests": tests,
            "known_failure_report_identity": (
                None
                if known_failure_report is None
                else known_failure_report.identity.uri
            ),
            "test_phases": [
                phase
                for phase in phases
                if str(phase["phase"]).split(".", 1)[0] == "test"
            ],
        }
    )
    run_evidence = {
        "schema": RETAINED_HARNESS_RECEIPT_SCHEMA,
        "classification": "retained-legacy-parity",
        "native_component_generation": False,
        "native_component_acceptance": False,
        "project_id": project.definition.project_id,
        "project_revision_identity": project_revision.uri,
        "component_lock_identities": [item.uri for item in component_locks],
        "worker_id": worker_id,
        "worker_identity": worker_identity.uri,
        "platform": platform_observation,
        "runner_identity": retained_harness_runner_identity(inventory).uri,
        "lifecycle_request_identity": lifecycle_request_identity.uri,
        "lifecycle_plan_identity": lifecycle_plan_identity.uri,
        "lifecycle_command_identity": lifecycle_command_identity.uri,
        "workspace_admission_identity": admission_identity.uri,
        "workspace_runtime_identity": workspace_runtime_identity.uri,
        "test_report_identity": test_report_identity.uri,
        "source_cache_decision_identity": source_cache_decision_identity.uri,
        "source_cache_lifecycle_identity": source_cache_lifecycle_identity.uri,
        "tests": tests,
        "phases": phases,
    }
    result_identity = canonical_identity(run_evidence)
    evidence_by_kind = {
        "lifecycle-command": lifecycle_command_identity,
        "lifecycle-plan": lifecycle_plan_identity,
        "lifecycle-request": lifecycle_request_identity,
        "observation-result": result_identity,
        "source-cache-decision": source_cache_decision_identity,
        "source-cache-lifecycle": source_cache_lifecycle_identity,
        "test-report": test_report_identity,
        "test-runner": retained_harness_runner_identity(inventory),
        "workspace-admission": admission_identity,
    }
    for phase_kind, evidence_kind in (
        ("build", "build-result"),
        ("package", "package-result"),
    ):
        selected = [
            phase
            for phase in phases
            if str(phase["phase"]).split(".", 1)[0] == phase_kind
        ]
        if selected:
            evidence_by_kind[evidence_kind] = canonical_identity(
                {
                    "schema": f"literate-ai/retained-{phase_kind}-result@1",
                    "request_identity": lifecycle_request_identity.uri,
                    "phases": selected,
                }
            )
    suite_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-legacy-parity-suite@1",
            "suite_id": RETAINED_HARNESS_SUITE_ID,
            "suite_version": RETAINED_HARNESS_SUITE_VERSION,
            "runner_identity": retained_harness_runner_identity(inventory).uri,
            "workspace_admission_identity": admission_identity.uri,
            "lifecycle_plan_identity": lifecycle_plan_identity.uri,
        }
    )
    receipt = ProjectTestReceipt(
        project_id=project.definition.project_id,
        project_revision_identity=project_revision,
        subject_identity=canonical_identity(
            {
                "schema": "literate-ai/retained-harness-subject@1",
                "project_revision_identity": project_revision.uri,
                "source_scope_identity": inventory["source_scope"]["paths_identity"],
                "source_tree_identity": retained_source_tree["identity"],
                "workspace_admission_identity": admission_identity.uri,
            }
        ),
        suite=VersionedContentRef(
            "test-suite",
            RETAINED_HARNESS_SUITE_ID,
            RETAINED_HARNESS_SUITE_VERSION,
            suite_identity,
        ),
        outcome="passed",
        summary=ProjectTestSummary(tests, tests, 0, 0),
        result_identity=result_identity,
        evidence=tuple(
            ProjectTestEvidence(kind, identity)
            for kind, identity in sorted(evidence_by_kind.items())
        ),
    )
    provisional = ProjectTestReceiptProvisional(
        lifecycle_request_identity=lifecycle_request_identity,
        lifecycle_command_identity=lifecycle_command_identity,
        source_cache_control_identity=source_cache_control_identity,
        component_lock_identities=component_locks,
        receipt_identity=receipt.identity,
        receipt=receipt,
    )
    finalized = ProjectTestReceiptFinalizedCandidate.finalize(
        provisional,
        source_cache_decision_identity=source_cache_decision_identity,
        source_cache_lifecycle_identity=source_cache_lifecycle_identity,
    )
    _require_project_unchanged(project, base_revision, component_locks)
    evidence_file_identity = _write_new_json(evidence_destination, run_evidence)
    try:
        write_project_test_receipt_finalized_candidate(candidate, finalized)
    except ProjectError as exc:
        _remove_owned_file(evidence_destination, evidence_file_identity)
        _error(exc.code, exc.message)
    return {
        "schema": RETAINED_HARNESS_RECEIPT_SCHEMA,
        "classification": "retained-legacy-parity",
        "candidate": str(candidate),
        "candidate_identity": finalized.identity.uri,
        "evidence": str(evidence_destination),
        "evidence_identity": result_identity.uri,
        "project_revision_identity": project_revision.uri,
        "worker_identity": worker_identity.uri,
        "runner_identity": retained_harness_runner_identity(inventory).uri,
        "workspace_runtime_identity": workspace_runtime_identity.uri,
        "suite_identity": suite_identity.uri,
        "tests": tests,
        "native_component_generation": False,
        "native_component_acceptance": False,
    }


def load_known_failure_report(path: Path) -> KnownTestFailureReport:
    """Load one canonical report for optional exact retained-test accounting."""

    document, canonical = _read_canonical_json(path, "known-failure report")
    try:
        report = KnownTestFailureReport.from_dict(document)
    except (TypeError, ValueError):
        _error(
            "retained_receipt.known_failure_report_invalid",
            "known-failure report is not a valid typed report",
        )
    if canonical_json_bytes(report.to_dict()) != canonical:
        _error(
            "retained_receipt.known_failure_report_invalid",
            "known-failure report is not canonical JSON",
        )
    return report


def _inventory_phase_kinds(inventory: dict[str, object]) -> frozenset[str]:
    stages = inventory.get("stages")
    if not isinstance(stages, list):
        _error("retained_receipt.inventory_invalid", "harness inventory has no stages")
    result: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("id"), str):
            _error(
                "retained_receipt.inventory_invalid",
                "harness inventory contains an invalid stage",
            )
        result.add(str(stage["id"]).split(".", 1)[0])
    return frozenset(result)


def _stage_plan(inventory: dict[str, object]) -> list[dict[str, str]]:
    stages = inventory.get("stages")
    assert isinstance(stages, list)
    plan: list[dict[str, str]] = []
    covered = stages_covered_by_root(inventory)
    for raw in stages:
        if not isinstance(raw, dict):
            continue
        stage_id = raw.get("id")
        if not isinstance(stage_id, str) or stage_id.split(".", 1)[0] not in {
            "build",
            "test",
            "package",
            "ci",
        }:
            continue
        if stage_id in covered:
            continue
        command = raw.get("command")
        evidence = raw.get("evidence")
        cwd = raw.get("cwd", ".")
        if not all(isinstance(item, str) and item for item in (command, evidence, cwd)):
            _error(
                "retained_receipt.inventory_invalid",
                "admitted retained stage is incomplete",
            )
        plan.append(
            {
                "id": stage_id,
                "command": command,
                "cwd": cwd,
                "evidence": evidence,
            }
        )
    return plan


def _load_retained_documents(
    project: LoadedProject,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, relative in (
        ("inventory", _INVENTORY_PATH),
        ("baseline", _BASELINE_PATH),
        ("parity", _PARITY_PATH),
        ("lift_shift", _LIFT_SHIFT_PATH),
    ):
        value, canonical = _read_canonical_json(project.root / relative, name)
        result[name] = {
            "value": value,
            "identity": canonical_identity(value).uri,
            "bytes": canonical,
        }
    inventory = result["inventory"]["value"]
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema") != HARNESS_INVENTORY_SCHEMA
    ):
        _error(
            "retained_receipt.inventory_invalid",
            "retained harness inventory has an unsupported schema",
        )
    _inventory_phase_kinds(inventory)
    wrapper = project.root / _WRAPPER_PATH
    try:
        if wrapper.is_symlink() or not wrapper.is_file():
            raise OSError
        content = wrapper.read_bytes()
    except OSError:
        _error(
            "retained_receipt.wrapper_invalid",
            "retained harness wrapper is unavailable",
        )
    result["wrapper"] = {
        "identity": canonical_identity(
            {
                "schema": "literate-ai/retained-harness-wrapper@1",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        ).uri,
        "bytes": content,
    }
    return result


def _require_qualified_parity(documents: dict[str, dict[str, Any]]) -> None:
    baseline = documents["baseline"]["value"]
    parity = documents["parity"]["value"]
    lift_shift = documents["lift_shift"]["value"]
    if not isinstance(baseline, dict) or baseline.get("state") != "passed":
        _error(
            "retained_receipt.baseline_unqualified",
            "retained harness direct baseline has not passed",
        )
    if (
        not isinstance(parity, dict)
        or parity.get("schema") != HARNESS_PARITY_SCHEMA
        or parity.get("state") != "passed"
    ):
        _error(
            "retained_receipt.parity_unqualified",
            "retained wrapper parity has not passed",
        )
    if not isinstance(lift_shift, dict) or lift_shift.get("state") != "passed":
        _error(
            "retained_receipt.lift_shift_unqualified",
            "retained implementation lift-and-shift has not passed",
        )


def _implementation_root(
    project: LoadedProject, documents: dict[str, dict[str, Any]]
) -> Path:
    lift_shift = documents["lift_shift"]["value"]
    assert isinstance(lift_shift, dict)
    raw = lift_shift.get("implementation_directory")
    if not isinstance(raw, str):
        _error(
            "retained_receipt.lift_shift_invalid",
            "lift-and-shift evidence omits the retained implementation",
        )
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        _error(
            "retained_receipt.lift_shift_invalid",
            "retained implementation path is unsafe",
        )
    candidate = project.root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project.root)
    except (OSError, ValueError):
        _error(
            "retained_receipt.lift_shift_invalid",
            "retained implementation path escapes the project",
        )
    if candidate.is_symlink() or not resolved.is_dir():
        _error(
            "retained_receipt.lift_shift_invalid",
            "retained implementation must be one project directory",
        )
    return resolved


def _platform_observation() -> dict[str, str]:
    return {
        "operating_system": platform.system().casefold(),
        "operating_system_release": platform.release(),
        "machine": platform.machine().casefold(),
        "python_implementation": platform.python_implementation().casefold(),
        "python_version": platform.python_version(),
    }


def _sanitized_phases(report: dict[str, object]) -> list[dict[str, object]]:
    phases = report.get("phases")
    if not isinstance(phases, list):
        _error(
            "retained_receipt.execution_invalid",
            "retained harness result omits phase evidence",
        )
    return [
        {
            key: value
            for key, value in phase.items()
            if key not in {"stdout_excerpt", "stderr_excerpt"}
        }
        for phase in phases
        if isinstance(phase, dict)
    ]


def _strict_test_total(
    phases: list[dict[str, object]],
    *,
    project_revision: ContentIdentity,
    worker_identity: ContentIdentity,
    known_failure_report: KnownTestFailureReport | None,
) -> int:
    observations = [
        phase.get("test_collection")
        for phase in phases
        if str(phase.get("phase", "")).split(".", 1)[0] == "test"
    ]
    counted = [
        observation
        for observation in observations
        if isinstance(observation, dict)
        and observation.get("state") == "nonempty"
        and isinstance(observation.get("total"), int)
    ]
    observed_total = sum(int(observation["total"]) for observation in counted)
    if known_failure_report is None:
        if len(counted) != len(observations) or observed_total < 1:
            _error(
                "retained_receipt.test_count_unreported",
                "every retained test phase must report an exact non-empty count",
            )
        return observed_total
    summary = known_failure_report.summary
    if (
        known_failure_report.suite != RETAINED_HARNESS_SUITE_ID
        or known_failure_report.pin_identity != project_revision
        or known_failure_report.context_identity != worker_identity
    ):
        _error(
            "retained_receipt.known_failure_report_mismatch",
            "known-failure report does not bind this suite, revision, and worker",
        )
    if (
        not known_failure_report.release_evidence
        or summary.selected < 1
        or summary.passed != summary.selected
        or summary.failed
        or summary.known_failed
        or summary.skipped
    ):
        _error(
            "retained_receipt.tests_not_all_passing",
            "known-failure accounting contains a failed, skipped, or known-failure "
            "outcome",
        )
    if counted and observed_total != summary.selected:
        _error(
            "retained_receipt.test_count_mismatch",
            "runner output and known-failure report disagree on the test total",
        )
    return summary.selected


def _require_project_unchanged(
    project: LoadedProject,
    retained_base_revision: ContentIdentity,
    component_locks: tuple[ContentIdentity, ...],
) -> None:
    try:
        validated_revision = validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        current_locks = current_project_component_lock_identities(project)
        wrapper_lock_identity = _current_wrapper_lock_identity(project)
    except (ProjectValidationError, ProjectComponentLockSetError) as exc:
        _error(
            getattr(exc, "code", "retained_receipt.project_changed"),
            getattr(exc, "message", str(exc)),
        )
    current_revision = retained_harness_project_authority_identity(
        project, validated_revision
    )
    if (
        current_revision != retained_base_revision
        or current_locks != component_locks
        or wrapper_lock_identity not in current_locks
    ):
        _error(
            "retained_receipt.project_changed",
            "project authority, retained source, or Component locks changed during "
            "retained execution",
        )


def _current_wrapper_lock_identity(project: LoadedProject) -> ContentIdentity:
    """Require the converted project's own wrapper lock, not an ambient catalog lock."""

    component = project.root.joinpath(*Path(_WRAPPER_COMPONENT_PATH).parts)
    if component.is_symlink() or not component.is_dir():
        _error(
            "retained_receipt.wrapper_component_missing",
            "retained execution requires the canonical legacy wrapper Component",
        )
    try:
        return current_component_lock_identity(component)
    except ProjectComponentLockSetError as exc:
        _error(
            "retained_receipt.wrapper_lock_invalid",
            "retained execution requires its current legacy wrapper lock: "
            f"{exc.message}",
        )


def _retained_project_authority_observation(
    project: LoadedProject,
    validated_revision: ContentIdentity,
    *,
    documents: dict[str, dict[str, Any]] | None = None,
    implementation: Path | None = None,
) -> tuple[ContentIdentity, dict[str, Any]]:
    selected_documents = documents or _load_retained_documents(project)
    _require_qualified_parity(selected_documents)
    selected_implementation = implementation or _implementation_root(
        project, selected_documents
    )
    inventory = selected_documents["inventory"]["value"]
    assert isinstance(inventory, dict)
    try:
        source_tree = observe_retained_tree(
            selected_implementation, inventory.get("source_scope")
        )["source_tree"]
    except (OSError, ValueError) as exc:
        _error(
            "retained_receipt.source_authority_invalid",
            f"retained authored-source authority is invalid: {exc}",
        )
    admission = _workspace_admission_identity(selected_documents)
    return (
        canonical_identity(
            {
                "schema": "literate-ai/retained-project-authority@1",
                "validated_project_authority_identity": validated_revision.uri,
                "workspace_admission_identity": admission.uri,
                "source_tree_identity": source_tree["identity"],
            }
        ),
        source_tree,
    )


def _workspace_admission_identity(
    documents: dict[str, dict[str, Any]],
) -> ContentIdentity:
    return canonical_identity(
        {
            "schema": "literate-ai/retained-harness-admission@1",
            "inventory": documents["inventory"]["identity"],
            "baseline": documents["baseline"]["identity"],
            "parity": documents["parity"]["identity"],
            "lift_shift": documents["lift_shift"]["identity"],
            "wrapper": documents["wrapper"]["identity"],
        }
    )


def _external_new_path(project: LoadedProject, path: Path, label: str) -> Path:
    requested = Path(path)
    try:
        if not requested.name or requested.is_symlink():
            raise OSError
        parent = requested.parent.resolve(strict=True)
        destination = parent / requested.name
        if not parent.is_dir() or destination.exists() or destination.is_symlink():
            raise OSError
        destination.relative_to(project.root.resolve(strict=True))
    except ValueError:
        return destination
    except (OSError, RuntimeError):
        _error(
            f"retained_receipt.{label}_path_invalid",
            f"{label} path must be one new file under an existing external directory",
        )
    _error(
        f"retained_receipt.{label}_inside_project",
        f"{label} path must remain outside project authority",
    )


def _read_canonical_json(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        content = path.read_bytes()
        if not 0 < len(content) <= _MAX_EVIDENCE_DOCUMENT_BYTES:
            raise OSError
        value = json.loads(content.decode("utf-8"))
        canonical = canonical_json_bytes(value)
        if content not in {canonical, canonical + b"\n"}:
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        _error(
            "retained_receipt.evidence_invalid",
            f"{label} must be one bounded canonical JSON file",
        )
    return value, canonical


def _write_new_json(path: Path, value: object) -> tuple[int, int]:
    content = canonical_json_bytes(value) + b"\n"
    temporary: Path | None = None
    committed_identity: tuple[int, int] | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".litai-retained-evidence-", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary_stat = temporary.stat()
        committed_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        if path.exists() or path.is_symlink():
            raise OSError
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
        exposed_stat = path.stat()
        if (exposed_stat.st_dev, exposed_stat.st_ino) != committed_identity:
            raise OSError
        observed, canonical = _read_canonical_json(path, "retained run evidence")
        if observed != value or canonical + b"\n" != content:
            raise OSError
        _fsync_directory(path.parent)
        return committed_identity
    except (OSError, RetainedHarnessReceiptError) as exc:
        if committed_identity is not None:
            _remove_owned_file(path, committed_identity)
        raise RetainedHarnessReceiptError(
            "retained_receipt.evidence_write_failed",
            "retained run evidence could not be atomically written",
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _remove_owned_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        if path.is_symlink():
            return
        observed = path.stat()
        if (observed.st_dev, observed.st_ino) == identity:
            path.unlink()
            _fsync_directory(path.parent)
    except OSError:
        return


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _error(code: str, message: str) -> None:
    raise RetainedHarnessReceiptError(code, message)


__all__ = [
    "RETAINED_HARNESS_RECEIPT_SCHEMA",
    "RETAINED_HARNESS_RUNNER_SCHEMA",
    "RETAINED_HARNESS_SUITE_ID",
    "RETAINED_HARNESS_SUITE_VERSION",
    "RetainedHarnessReceiptError",
    "load_known_failure_report",
    "retained_harness_receipt_policy",
    "retained_harness_project_authority_identity",
    "retained_harness_runner_identity",
    "retained_harness_worker_identity",
    "run_retained_harness_receipt",
]
