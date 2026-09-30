"""Safe project storage for one replaceable, Git-history-friendly test receipt."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from literate_ai.contracts import (
    PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA,
    PROJECT_TEST_RECEIPT_PROVISIONAL_SCHEMA,
    PROJECT_TEST_RECEIPT_SCHEMA,
    PROJECT_TEST_RUNNER_EVIDENCE_KIND,
    ContentIdentity,
    ContractValidationError,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    canonical_json_bytes,
    rebuild_project_authority_identity,
)
from literate_ai.projects import LoadedProject, ProjectError

MAXIMUM_PROJECT_TEST_RECEIPT_BYTES = 256 * 1024


def _read_bounded(
    path: Path,
    *,
    code: str,
    label: str,
    maximum_bytes: int = MAXIMUM_PROJECT_TEST_RECEIPT_BYTES,
) -> bytes:
    candidate = Path(os.path.abspath(path))
    descriptor: int | None = None
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        named = os.stat(candidate, follow_symlinks=False)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            len(content) != before.st_size
            or len(content) > maximum_bytes
            or before_identity != after_identity
            or not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise OSError
    except OSError as exc:
        raise ProjectError(
            code, f"{label} must be one stable bounded regular file"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return content


def _parse(content: bytes, *, code: str, label: str) -> ProjectTestReceipt:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProjectError(
            code, f"{label} is not a valid project test receipt"
        ) from exc
    if (
        isinstance(value, dict)
        and value.get("schema") == PROJECT_TEST_RECEIPT_PROVISIONAL_SCHEMA
    ):
        raise ProjectError(
            "project.test_receipt_provisional_unfinalized",
            "a provisional lifecycle assertion cannot be promoted as a project test "
            "receipt; only litai rebuild may finalize it after outer side effects",
        )
    try:
        return ProjectTestReceipt.from_dict(value)
    except (ContractValidationError, RecursionError) as exc:
        raise ProjectError(
            code, f"{label} is not a valid project test receipt"
        ) from exc


def _parse_tracked(
    content: bytes, *, code: str, label: str
) -> tuple[ProjectTestReceipt, ProjectTestReceiptFinalizedCandidate | None]:
    """Parse either legitimate tracked receipt boundary without losing its envelope."""

    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProjectError(
            code, f"{label} is not a valid project test receipt"
        ) from exc
    schema = value.get("schema") if isinstance(value, dict) else None
    if schema == PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA:
        try:
            finalized = ProjectTestReceiptFinalizedCandidate.from_dict(value)
        except (ContractValidationError, RecursionError) as exc:
            raise ProjectError(
                code, f"{label} is not a valid finalized project test receipt"
            ) from exc
        return finalized.receipt, finalized
    return _parse(content, code=code, label=label), None


def _parse_current_map(content: bytes):
    """Recognize canonical indexes without treating their references as proof."""

    from literate_ai.security.evidence.current import CurrentEvidenceMap

    try:
        value = json.loads(content)
        if (
            not isinstance(value, dict)
            or value.get("schema") != CurrentEvidenceMap.SCHEMA
        ):
            return None
        current = CurrentEvidenceMap.from_dict(value)
        if content != canonical_json_bytes(current.to_dict()) + b"\n":
            raise ValueError("current map is not canonical")
        return current
    except (ValueError, RecursionError) as error:
        raise ProjectError(
            "project.test_receipt_invalid", "current receipt is not canonical JSON"
        ) from error


def _canonical_tracked_bytes(
    receipt: ProjectTestReceipt,
    finalized: ProjectTestReceiptFinalizedCandidate | None,
) -> bytes:
    return (
        _canonical_bytes(receipt)
        if finalized is None
        else _canonical_finalized_candidate_bytes(finalized)
    )


def _tracked_project_revision(
    project: LoadedProject,
    base_revision: ContentIdentity,
    finalized: ProjectTestReceiptFinalizedCandidate | None,
) -> ContentIdentity:
    if finalized is None:
        return base_revision
    policy = project.definition.test_receipt_policy
    if policy is not None:
        from literate_ai.adapters.retained_harness_receipts import (
            RETAINED_HARNESS_SUITE_ID,
            RetainedHarnessReceiptError,
            retained_harness_project_authority_identity,
        )

        if policy.suite_id == RETAINED_HARNESS_SUITE_ID:
            try:
                base_revision = retained_harness_project_authority_identity(
                    project, base_revision
                )
            except RetainedHarnessReceiptError as exc:
                raise ProjectError(exc.code, exc.message) from exc
    return rebuild_project_authority_identity(
        base_revision, finalized.component_lock_identities
    )


def _parse_provisional(
    content: bytes, *, code: str, label: str
) -> ProjectTestReceiptProvisional:
    try:
        value = json.loads(content.decode("utf-8"))
        return ProjectTestReceiptProvisional.from_dict(value)
    except (
        UnicodeError,
        json.JSONDecodeError,
        ContractValidationError,
        RecursionError,
    ) as exc:
        raise ProjectError(
            code, f"{label} is not a valid provisional project test receipt"
        ) from exc


def _parse_finalized_candidate(
    content: bytes, *, code: str, label: str
) -> ProjectTestReceiptFinalizedCandidate:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProjectError(code, f"{label} is not valid canonical JSON") from exc
    schema = value.get("schema") if isinstance(value, dict) else None
    if schema == PROJECT_TEST_RECEIPT_PROVISIONAL_SCHEMA:
        raise ProjectError(
            "project.test_receipt_provisional_unfinalized",
            "a provisional lifecycle assertion cannot cross the supported API/TCB "
            "boundary; only litai rebuild may finalize it after outer validations",
        )
    if schema == PROJECT_TEST_RECEIPT_SCHEMA:
        raise ProjectError(
            "project.test_receipt_candidate_unfinalized",
            "a raw receipt cannot cross the supported API/TCB boundary; provide the "
            "finalized candidate envelope emitted by litai rebuild",
        )
    if schema != PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA:
        raise ProjectError(code, f"{label} is not a finalized receipt candidate")
    try:
        return ProjectTestReceiptFinalizedCandidate.from_dict(value)
    except (ContractValidationError, RecursionError) as exc:
        raise ProjectError(code, f"{label} is not a valid finalized candidate") from exc


def _canonical_bytes(receipt: ProjectTestReceipt) -> bytes:
    return canonical_json_bytes(receipt.to_dict()) + b"\n"


def _canonical_provisional_bytes(
    provisional: ProjectTestReceiptProvisional,
) -> bytes:
    return canonical_json_bytes(provisional.to_dict()) + b"\n"


def _canonical_finalized_candidate_bytes(
    candidate: ProjectTestReceiptFinalizedCandidate,
) -> bytes:
    return canonical_json_bytes(candidate.to_dict()) + b"\n"


def _target(project: LoadedProject, *, create_parent: bool) -> Path | None:
    relative = project.definition.test_receipt
    if relative is None:
        return None
    target = project.root.joinpath(*Path(relative).parts)
    current = project.root
    for part in Path(relative).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ProjectError(
                "project.test_receipt_path_invalid",
                "project test receipt path traverses a symbolic link",
            )
        if current.exists() and not current.is_dir():
            raise ProjectError(
                "project.test_receipt_path_invalid",
                "project test receipt parent must be a regular directory",
            )
        if create_parent and not current.exists():
            try:
                current.mkdir()
            except OSError as exc:
                raise ProjectError(
                    "project.test_receipt_path_invalid",
                    "project test receipt parent cannot be created",
                ) from exc
    if create_parent and not target.parent.resolve(strict=True).is_relative_to(
        project.root
    ):
        raise ProjectError(
            "project.test_receipt_path_invalid",
            "project test receipt path escapes the project",
        )
    if target.is_symlink():
        raise ProjectError(
            "project.test_receipt_path_invalid",
            "project test receipt cannot be a symbolic link",
        )
    return target


def _require_project_binding(
    project: LoadedProject,
    receipt: ProjectTestReceipt,
    project_revision_identity: ContentIdentity,
) -> None:
    if receipt.project_id != project.definition.project_id:
        raise ProjectError(
            "project.test_receipt_project_mismatch",
            "project test receipt identifies another project",
        )
    if receipt.project_revision_identity != project_revision_identity:
        raise ProjectError(
            "project.test_receipt_project_mismatch",
            "project test receipt does not bind the current project authority revision",
        )


def _configured_policy(project: LoadedProject) -> ProjectTestReceiptPolicy:
    if project.definition.test_receipt is None:
        raise ProjectError(
            "project.test_receipt_unconfigured",
            "project definition does not configure test_receipt",
        )
    policy = project.definition.test_receipt_policy
    if policy is None:
        raise ProjectError(
            "project.test_receipt_policy_unconfigured",
            "project definition does not configure test_receipt_policy; no receipt "
            "can be accepted or current",
        )
    return policy


def _policy_mismatch(
    policy: ProjectTestReceiptPolicy, receipt: ProjectTestReceipt
) -> str | None:
    if receipt.suite.identifier != policy.suite_id:
        return "project test receipt suite ID is not authorized by project policy"
    if receipt.suite.version != policy.suite_version:
        return "project test receipt suite version is not authorized by project policy"
    if receipt.summary.total < policy.minimum_test_count:
        return "project test receipt contains fewer tests than project policy requires"
    evidence = {item.kind: item.identity for item in receipt.evidence}
    missing = set(policy.required_evidence_kinds) - evidence.keys()
    if missing:
        return "project test receipt lacks required evidence: " + ", ".join(
            sorted(missing)
        )
    if evidence[PROJECT_TEST_RUNNER_EVIDENCE_KIND] != policy.runner_identity:
        return (
            "project test receipt runner identity is not authorized by project policy"
        )
    return None


def _require_policy_binding(
    project: LoadedProject, receipt: ProjectTestReceipt
) -> ProjectTestReceiptPolicy:
    policy = _configured_policy(project)
    mismatch = _policy_mismatch(policy, receipt)
    if mismatch is not None:
        raise ProjectError("project.test_receipt_policy_mismatch", mismatch)
    return policy


def inspect_project_test_receipt(
    project: LoadedProject, *, project_revision_identity: ContentIdentity
) -> dict[str, object]:
    """Describe the configured receipt without requiring one for new projects."""

    target = _target(project, create_parent=False)
    if target is None:
        return {"configured": False, "state": "unconfigured"}
    relative = target.relative_to(project.root).as_posix()
    policy = project.definition.test_receipt_policy
    if not target.exists() and not target.is_symlink():
        result: dict[str, object] = {
            "configured": True,
            "path": relative,
            "policy_configured": policy is not None,
            "state": "policy-unconfigured" if policy is None else "missing",
        }
        if policy is not None:
            result["policy_identity"] = policy.identity.uri
        return result
    content = _read_bounded(
        target,
        code="project.test_receipt_invalid",
        label="configured project test receipt",
    )
    current_map = _parse_current_map(content)
    if current_map is not None:
        return {
            "configured": True,
            "path": relative,
            "policy_configured": policy is not None,
            "state": "authentication-required",
            "authenticated": False,
            "current_map_identity": current_map.identity.uri,
            "project_revision_identity": current_map.project_authority.uri,
            "current_project_revision_identity": project_revision_identity.uri,
        }
    receipt, finalized = _parse_tracked(
        content,
        code="project.test_receipt_invalid",
        label="configured project test receipt",
    )
    if content != _canonical_tracked_bytes(receipt, finalized):
        raise ProjectError(
            "project.test_receipt_invalid",
            "configured project test receipt is not canonical JSON",
        )
    current_revision = _tracked_project_revision(
        project, project_revision_identity, finalized
    )
    project_current = (
        receipt.project_id == project.definition.project_id
        and receipt.project_revision_identity == current_revision
    )
    policy_mismatch = None if policy is None else _policy_mismatch(policy, receipt)
    state = (
        "policy-unconfigured"
        if policy is None
        else "stale"
        if not project_current
        else "policy-mismatch"
        if policy_mismatch is not None
        else "current"
    )
    result = {
        "configured": True,
        "path": relative,
        "policy_configured": policy is not None,
        "state": state,
        "authenticated": False,
        "identity": receipt.identity.uri,
        "project_revision_identity": receipt.project_revision_identity.uri,
        "current_project_revision_identity": current_revision.uri,
        "subject_identity": receipt.subject_identity.uri,
        "suite_identity": receipt.suite.content_identity.uri,
        "result_identity": receipt.result_identity.uri,
        "summary": receipt.summary.to_dict(),
    }
    if policy is not None:
        result["policy_identity"] = policy.identity.uri
    if finalized is not None:
        result["finalized_candidate_identity"] = finalized.identity.uri
        result["component_lock_count"] = len(finalized.component_lock_identities)
    return result


def load_project_test_receipt_candidate(
    path: Path,
) -> tuple[ProjectTestReceiptFinalizedCandidate, bytes]:
    """Load only an outer-finalized candidate at the supported API/TCB boundary."""

    content = _read_bounded(
        path,
        code="project.test_receipt_candidate_invalid",
        label="project test receipt candidate",
    )
    candidate = _parse_finalized_candidate(
        content,
        code="project.test_receipt_candidate_invalid",
        label="project test receipt candidate",
    )
    return candidate, _canonical_finalized_candidate_bytes(candidate)


def load_project_test_receipt_provisional(
    path: Path,
) -> tuple[ProjectTestReceiptProvisional, bytes]:
    """Load one typed driver assertion that public receipt promotion rejects."""

    content = _read_bounded(
        path,
        code="project.test_receipt_provisional_invalid",
        label="provisional project test receipt",
    )
    provisional = _parse_provisional(
        content,
        code="project.test_receipt_provisional_invalid",
        label="provisional project test receipt",
    )
    return provisional, _canonical_provisional_bytes(provisional)


def write_project_test_receipt_provisional(
    path: Path, provisional: ProjectTestReceiptProvisional
) -> None:
    """Write one canonical provisional assertion to a new regular file."""

    if not isinstance(provisional, ProjectTestReceiptProvisional):
        raise TypeError("provisional receipt must be ProjectTestReceiptProvisional")
    destination = Path(path)
    content = _canonical_provisional_bytes(provisional)
    descriptor: int | None = None
    try:
        if destination.is_symlink() or not destination.parent.is_dir():
            raise OSError
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ProjectError(
            "project.test_receipt_provisional_write_failed",
            "provisional project test receipt must be written exactly once",
        ) from exc


def write_project_test_receipt_finalized_candidate(
    path: Path, candidate: ProjectTestReceiptFinalizedCandidate
) -> None:
    """Atomically expose one finalized candidate at a new external path."""

    if not isinstance(candidate, ProjectTestReceiptFinalizedCandidate):
        raise TypeError("candidate must be a ProjectTestReceiptFinalizedCandidate")
    destination = Path(path)
    content = _canonical_finalized_candidate_bytes(candidate)
    temporary: Path | None = None
    committed_identity: tuple[int, int] | None = None
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
            prefix=".litai-retained-candidate-", dir=parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        observed = temporary.stat()
        committed_identity = (observed.st_dev, observed.st_ino)
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
        observed = destination.stat()
        if (observed.st_dev, observed.st_ino) != committed_identity:
            raise OSError
        loaded, canonical = load_project_test_receipt_candidate(destination)
        if loaded != candidate or canonical != content:
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
        raise ProjectError(
            "project.test_receipt_candidate_write_failed",
            "finalized project test receipt candidate must be atomically written "
            "to one new external file",
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_project_test_receipt_provisional(
    project: LoadedProject,
    provisional_path: Path,
    *,
    project_revision_identity: ContentIdentity,
    lifecycle_request_identity: ContentIdentity,
    lifecycle_command_identity: ContentIdentity,
    source_cache_control_identity: ContentIdentity,
    component_lock_identities: tuple[ContentIdentity, ...],
) -> ProjectTestReceiptProvisional:
    """Validate a driver assertion without making its nested receipt promotable."""

    provisional, _candidate = load_project_test_receipt_provisional(provisional_path)
    try:
        provisional.validate_finalization_context(
            lifecycle_request_identity=lifecycle_request_identity,
            lifecycle_command_identity=lifecycle_command_identity,
            source_cache_control_identity=source_cache_control_identity,
            component_lock_identities=component_lock_identities,
        )
    except ContractValidationError as exc:
        raise ProjectError(
            "project.test_receipt_provisional_binding_mismatch",
            "provisional receipt does not bind the exact outer finalization context",
        ) from exc
    _require_project_binding(project, provisional.receipt, project_revision_identity)
    _require_policy_binding(project, provisional.receipt)
    return provisional


def validate_project_test_receipt_candidate(
    project: LoadedProject,
    candidate_path: Path,
    *,
    project_revision_identity: ContentIdentity,
) -> ProjectTestReceipt:
    """Validate one external passing candidate without committing it to the project."""

    finalized, _candidate = load_project_test_receipt_candidate(candidate_path)
    receipt = finalized.receipt
    _require_project_binding(project, receipt, project_revision_identity)
    _require_policy_binding(project, receipt)
    return receipt


def update_project_test_receipt(
    project: LoadedProject,
    candidate_path: Path,
    *,
    project_revision_identity: ContentIdentity,
) -> dict[str, object]:
    """Atomically replace the configured receipt with one valid passing candidate."""

    finalized, candidate = load_project_test_receipt_candidate(candidate_path)
    receipt = finalized.receipt
    exact_revision = _tracked_project_revision(
        project, project_revision_identity, finalized
    )
    _require_project_binding(project, receipt, exact_revision)
    _require_policy_binding(project, receipt)
    return _update_project_test_receipt_bytes(project, receipt, finalized, candidate)


def update_project_test_receipt_value(
    project: LoadedProject,
    receipt: ProjectTestReceipt,
    *,
    project_revision_identity: ContentIdentity,
) -> dict[str, object]:
    """Atomically commit one in-memory passing receipt after policy validation."""

    if not isinstance(receipt, ProjectTestReceipt):
        raise TypeError("receipt must be a ProjectTestReceipt")
    _require_project_binding(project, receipt, project_revision_identity)
    _require_policy_binding(project, receipt)
    return _update_project_test_receipt_bytes(
        project, receipt, None, _canonical_bytes(receipt)
    )


def validate_project_test_receipt_finalized_value(
    project: LoadedProject,
    finalized: ProjectTestReceiptFinalizedCandidate,
    *,
    project_revision_identity: ContentIdentity,
) -> ProjectTestReceipt:
    """Validate exact finalization, project and suite scope without publication."""

    if not isinstance(finalized, ProjectTestReceiptFinalizedCandidate):
        raise TypeError("finalized must be a ProjectTestReceiptFinalizedCandidate")
    receipt = finalized.receipt
    exact_revision = _tracked_project_revision(
        project, project_revision_identity, finalized
    )
    _require_project_binding(project, receipt, exact_revision)
    _require_policy_binding(project, receipt)
    return receipt


def update_project_test_receipt_finalized_value(
    project: LoadedProject,
    finalized: ProjectTestReceiptFinalizedCandidate,
    *,
    project_revision_identity: ContentIdentity,
) -> dict[str, object]:
    """Commit one in-memory receipt with its exact outer finalization context."""

    receipt = validate_project_test_receipt_finalized_value(
        project,
        finalized,
        project_revision_identity=project_revision_identity,
    )
    return _update_project_test_receipt_bytes(
        project,
        receipt,
        finalized,
        _canonical_finalized_candidate_bytes(finalized),
    )


def _update_project_test_receipt_bytes(
    project: LoadedProject,
    receipt: ProjectTestReceipt,
    finalized: ProjectTestReceiptFinalizedCandidate | None,
    candidate: bytes,
) -> dict[str, object]:
    target = _target(project, create_parent=True)
    if target is None:
        raise ProjectError(
            "project.test_receipt_unconfigured",
            "project definition does not configure test_receipt",
        )
    before: bytes | None = None
    if target.exists() or target.is_symlink():
        before = _read_bounded(
            target,
            code="project.test_receipt_invalid",
            label="configured project test receipt",
        )
        previous, previous_finalized = _parse_tracked(
            before,
            code="project.test_receipt_invalid",
            label="configured project test receipt",
        )
        if before != _canonical_tracked_bytes(previous, previous_finalized):
            raise ProjectError(
                "project.test_receipt_invalid",
                "configured project test receipt is not canonical JSON",
            )
        if before == candidate:
            return {
                "path": target.relative_to(project.root).as_posix(),
                "identity": receipt.identity.uri,
                "updated": False,
            }

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        current = None
        if target.exists() or target.is_symlink():
            current = _read_bounded(
                target,
                code="project.test_receipt_changed",
                label="configured project test receipt",
            )
        if current != before:
            raise ProjectError(
                "project.test_receipt_changed",
                "project test receipt changed during atomic replacement",
            )
        checked_target = _target(project, create_parent=False)
        if checked_target != target:
            raise ProjectError(
                "project.test_receipt_changed",
                "project test receipt target changed during atomic replacement",
            )
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    result: dict[str, object] = {
        "path": target.relative_to(project.root).as_posix(),
        "identity": receipt.identity.uri,
        "updated": True,
    }
    if finalized is not None:
        result["finalized_candidate_identity"] = finalized.identity.uri
    return result


def check_project_test_receipt(
    project: LoadedProject,
    candidate_path: Path,
    *,
    project_revision_identity: ContentIdentity,
) -> dict[str, object]:
    """Require the committed current receipt to equal one external candidate."""

    finalized, _candidate = load_project_test_receipt_candidate(candidate_path)
    receipt = finalized.receipt
    exact_revision = _tracked_project_revision(
        project, project_revision_identity, finalized
    )
    return _check_project_test_receipt_value(
        project,
        receipt,
        _canonical_finalized_candidate_bytes(finalized),
        exact_revision,
    )


def check_project_test_receipt_value(
    project: LoadedProject,
    receipt: ProjectTestReceipt,
    *,
    project_revision_identity: ContentIdentity,
) -> dict[str, object]:
    """Require the committed current receipt to equal one in-memory candidate."""

    return _check_project_test_receipt_value(
        project,
        receipt,
        _canonical_bytes(receipt),
        project_revision_identity,
    )


def _check_project_test_receipt_value(
    project: LoadedProject,
    receipt: ProjectTestReceipt,
    candidate: bytes,
    project_revision_identity: ContentIdentity,
) -> dict[str, object]:
    _require_project_binding(project, receipt, project_revision_identity)
    _require_policy_binding(project, receipt)
    target = _target(project, create_parent=False)
    if target is None:
        raise ProjectError(
            "project.test_receipt_unconfigured",
            "project definition does not configure test_receipt",
        )
    if not target.exists() and not target.is_symlink():
        raise ProjectError(
            "project.test_receipt_missing",
            "configured project test receipt has not been committed",
        )
    current = _read_bounded(
        target,
        code="project.test_receipt_invalid",
        label="configured project test receipt",
    )
    current_receipt, current_finalized = _parse_tracked(
        current,
        code="project.test_receipt_invalid",
        label="configured project test receipt",
    )
    if current != _canonical_tracked_bytes(current_receipt, current_finalized):
        raise ProjectError(
            "project.test_receipt_invalid",
            "configured project test receipt is not canonical JSON",
        )
    if current != candidate:
        raise ProjectError(
            "project.test_receipt_mismatch",
            "committed project test receipt differs from the tested candidate",
        )
    return {
        "path": target.relative_to(project.root).as_posix(),
        "identity": receipt.identity.uri,
        "matched": True,
    }


def require_current_project_test_receipt(
    project: LoadedProject, *, project_revision_identity: ContentIdentity
) -> dict[str, object]:
    """Require one canonical receipt bound to the full current authority revision."""

    inspection = inspect_project_test_receipt(
        project, project_revision_identity=project_revision_identity
    )
    state = inspection["state"]
    if state == "authentication-required":
        raise ProjectError(
            "project.test_receipt_authentication_required",
            "current evidence map requires fresh verification against independent "
            "plan, policy, revocations and signed evidence stores",
        )
    if state == "unconfigured":
        raise ProjectError(
            "project.test_receipt_unconfigured",
            "project definition does not configure test_receipt",
        )
    if state == "policy-unconfigured":
        raise ProjectError(
            "project.test_receipt_policy_unconfigured",
            "project definition does not configure test_receipt_policy; no receipt "
            "can be accepted or current",
        )
    if state == "missing":
        raise ProjectError(
            "project.test_receipt_missing",
            "configured project test receipt has not been committed",
        )
    if state == "stale":
        raise ProjectError(
            "project.test_receipt_stale",
            "configured project test receipt does not bind the current project "
            "authority revision",
        )
    if state == "policy-mismatch":
        raise ProjectError(
            "project.test_receipt_policy_mismatch",
            "configured project test receipt does not satisfy current project policy",
        )
    return inspection


__all__ = [
    "MAXIMUM_PROJECT_TEST_RECEIPT_BYTES",
    "check_project_test_receipt",
    "check_project_test_receipt_value",
    "inspect_project_test_receipt",
    "load_project_test_receipt_candidate",
    "load_project_test_receipt_provisional",
    "require_current_project_test_receipt",
    "update_project_test_receipt",
    "update_project_test_receipt_finalized_value",
    "update_project_test_receipt_value",
    "validate_project_test_receipt_candidate",
    "validate_project_test_receipt_finalized_value",
    "validate_project_test_receipt_provisional",
    "write_project_test_receipt_finalized_candidate",
    "write_project_test_receipt_provisional",
]
