"""Reviewed public plans over the live repository refresh transaction."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.repository_lineage import RepositoryFetchDeadlinePolicy
from literate_ai.contracts.repository_refresh import RepositoryRefreshRequest
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy

from .repository_local_tree import capture_local_repository_tree
from .repository_orchestration import OrchestrationInventoryError, _read_document
from .repository_publication import (
    PublishedRepositoryTree,
    capture_published_repository_tree,
)
from .repository_refresh import (
    prepare_repository_refresh,
    require_repository_refresh_inputs_unchanged,
)
from .repository_refresh_application import apply_repository_refresh
from .repository_refresh_ownership import (
    refresh_publication_custody_identity,
    reserve_published_refresh,
)
from .repository_refresh_publication import (
    PreparedRefreshPublication,
    _protected_roots,
    prepare_refresh_publication,
)
from .repository_refresh_reservations import refresh_custody_identity

PLAN_SCHEMA = "literate-ai/orchestration-refresh-plan@1"
CHECK_SCHEMA = "literate-ai/orchestration-refresh-check@1"
APPLY_SCHEMA = "literate-ai/orchestration-refresh-apply@1"
_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_REQUEST_BYTES = 1024 * 1024


def _fail(suffix: str, message: str) -> None:
    raise OrchestrationInventoryError("refresh_" + suffix, message) from None


def _is_windows() -> bool:
    return os.name == "nt"


def _application_support(
    publication: PreparedRefreshPublication,
) -> tuple[bool, str | None]:
    if not _is_windows():
        return True, None
    previous = {
        item.path: item.commit
        for item in publication.refresh.authority.previous.repositories
    }
    children = {
        item.root.relative_to(publication.refresh.repository.root).as_posix(): item
        for item in publication.refresh.children
    }
    for (path, endpoint), proof in zip(
        publication.endpoints, publication.observations, strict=True
    ):
        if dict(publication.target_modes)[path] == "root-pin-only":
            continue
        if previous[path] == proof.commit:
            continue
        require_repository_refresh_inputs_unchanged(publication.refresh)
        try:
            captured = capture_published_repository_tree(
                endpoint,
                proof.commit,
                deadline_policy=publication.deadline_policy,
                protected_roots=_protected_roots(publication.refresh),
            )
        finally:
            require_repository_refresh_inputs_unchanged(publication.refresh)
        if (
            not isinstance(captured, PublishedRepositoryTree)
            or captured.publication != proof
        ):
            _fail(
                "publication_changed",
                "platform preflight tree differs from reviewed publication",
            )
        require_repository_refresh_inputs_unchanged(publication.refresh)
        try:
            local = capture_local_repository_tree(
                children[path].root,
                children[path].commit,
                policy=RepositoryTreeCapturePolicy(),
            )
        finally:
            require_repository_refresh_inputs_unchanged(publication.refresh)
        if any(
            entry.mode == "120000"
            for tree in (local, captured.tree)
            for entry in tree.entries
        ):
            return False, "prospective-symlink-unsupported-on-windows"
        local_modes = {entry.path: entry.mode for entry in local.entries}
        prospective_modes = {entry.path: entry.mode for entry in captured.tree.entries}
        if any(
            (local_modes.get(path) == "040000")
            != (prospective_modes.get(path) == "040000")
            for path in local_modes.keys() | prospective_modes.keys()
        ):
            return False, "directory-transition-unsupported-on-windows"
    return True, None


def load_repository_refresh_request(path: Path) -> RepositoryRefreshRequest:
    """Read one exact canonical request without following an unsafe input path."""
    path = Path(path).absolute()
    try:
        raw = _read_document(
            path,
            label="repository refresh request",
            error_suffix="refresh_request_invalid",
            maximum_bytes=_MAX_REQUEST_BYTES,
        )
        value = json.loads(raw)
        request = RepositoryRefreshRequest.from_dict(value)
    except OrchestrationInventoryError:
        raise
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        json.JSONDecodeError,
        UnsafeFilesystemPathError,
    ) as exc:
        raise OrchestrationInventoryError(
            "refresh_request_invalid",
            "repository refresh request is unavailable or invalid",
        ) from exc
    if raw != canonical_json_bytes(request.to_dict()) + b"\n":
        _fail(
            "request_invalid",
            "repository refresh request must use exact canonical JSON bytes",
        )
    return request


@dataclass(frozen=True, slots=True)
class PreparedRepositoryRefreshPlan:
    publication: PreparedRefreshPublication
    document: dict[str, Any]


def _prepare_repository_refresh_plan(
    root: Path,
    request: RepositoryRefreshRequest,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy,
) -> PreparedRepositoryRefreshPlan:
    if not isinstance(request, RepositoryRefreshRequest):
        raise TypeError("refresh planning requires typed canonical intent")
    if not isinstance(deadline_policy, RepositoryFetchDeadlinePolicy):
        raise TypeError("refresh planning requires a typed deadline policy")
    refresh = prepare_repository_refresh(Path(root).absolute(), request)
    publication = prepare_refresh_publication(refresh, deadline_policy=deadline_policy)
    apply_supported, unavailable_reason = _application_support(publication)
    authority = refresh.authority
    changes = authority.to_dict()["changes"]
    target_modes = [
        {"path": path, "mode": mode} for path, mode in publication.target_modes
    ]
    source_writes = any(
        mode == "source-transition"
        and next(
            child
            for child in refresh.children
            if child.root.relative_to(refresh.repository.root).as_posix() == path
        ).commit
        != proof.commit
        for (path, mode), proof in zip(
            publication.target_modes, publication.observations, strict=True
        )
    )
    plan = {
        "schema": PLAN_SCHEMA,
        "mode": "read-only-plan",
        "profile": "git-submodule-orchestration-refresh",
        "request": request.to_dict(),
        "request_identity": request.identity,
        "previous_authority": authority.previous.to_dict(),
        "previous_authority_identity": authority.previous.identity,
        "prospective_authority": authority.prospective.to_dict(),
        "prospective_authority_identity": authority.prospective.identity,
        "changes": changes,
        "changed": bool(changes),
        "authority_review_required": bool(changes),
        "deadline_policy": deadline_policy.to_dict(),
        "deadline_policy_identity": deadline_policy.identity.uri,
        "publication": "verified",
        "publication_identity": publication.identity,
        "publication_targets": [
            {
                "path": path,
                "mode": mode,
                "repository_url": endpoint,
                "proof": asdict(proof),
                "proof_identity": proof.identity,
            }
            for (path, endpoint), proof, (_, mode) in zip(
                publication.endpoints,
                publication.observations,
                publication.target_modes,
                strict=True,
            )
        ],
        "target_modes": target_modes,
        "source_writes": source_writes,
        "refresh_custody_identity": refresh_custody_identity(refresh),
        "publication_custody_identity": refresh_publication_custody_identity(
            publication
        ),
        "apply_supported": apply_supported,
        "apply_unavailable_reason": unavailable_reason,
        "writes": False,
        "execution": False,
        "child_authority": "independent",
        "child_acceptance": "not-qualified",
        "crash_replay": "not-supported",
    }
    document = {**plan, "plan_identity": canonical_identity(plan).uri}
    return PreparedRepositoryRefreshPlan(publication, document)


def plan_repository_refresh(
    root: Path,
    request: RepositoryRefreshRequest,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy,
) -> dict[str, Any]:
    return _prepare_repository_refresh_plan(
        root, request, deadline_policy=deadline_policy
    ).document


def _require_plan_identity(value: str) -> None:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        _fail("plan_stale", "refresh requires an exact reviewed plan identity")


def check_repository_refresh(
    root: Path,
    request: RepositoryRefreshRequest,
    *,
    expected_plan_identity: str,
    deadline_policy: RepositoryFetchDeadlinePolicy,
) -> dict[str, Any]:
    _require_plan_identity(expected_plan_identity)
    prepared = _prepare_repository_refresh_plan(
        root, request, deadline_policy=deadline_policy
    )
    plan = prepared.document
    if plan["plan_identity"] != expected_plan_identity:
        _fail("plan_stale", "reviewed refresh plan no longer matches current inputs")
    return {
        "schema": CHECK_SCHEMA,
        "state": "current",
        "plan_identity": expected_plan_identity,
        "request_identity": plan["request_identity"],
        "publication_identity": plan["publication_identity"],
        "refresh_custody_identity": plan["refresh_custody_identity"],
        "publication_custody_identity": plan["publication_custody_identity"],
        "deadline_policy_identity": plan["deadline_policy_identity"],
        "authority_review_required": plan["authority_review_required"],
        "apply_supported": plan["apply_supported"],
        "apply_unavailable_reason": plan["apply_unavailable_reason"],
        "target_modes": plan["target_modes"],
        "source_writes": plan["source_writes"],
        "writes": False,
        "execution": False,
        "child_authority": "independent",
        "child_acceptance": "not-qualified",
    }


def _refresh_apply_result(plan, expected_plan_identity, applied):
    cleanup_retained = list(applied.cleanup_retained)
    authority_changed = applied.changed
    source_writes = applied.source_writes
    if source_writes is not plan["source_writes"]:
        _fail(
            "application_mismatch",
            "applied source-write mode differs from reviewed refresh plan",
        )
    filesystem_writes = authority_changed or source_writes or bool(cleanup_retained)
    result = {
        "schema": APPLY_SCHEMA,
        "state": (
            "committed-with-cleanup-retained"
            if cleanup_retained
            else "committed"
            if authority_changed or source_writes
            else "no-op"
        ),
        "transaction_state": applied.state,
        "changed": authority_changed,
        "authority_changed": authority_changed,
        "filesystem_writes": filesystem_writes,
        "cleanup_retained": cleanup_retained,
        "target_modes": plan["target_modes"],
        "source_writes": source_writes,
        "authority_review_required": authority_changed,
        "writes": filesystem_writes,
        "execution": False,
        "apply_supported": True,
        "plan_identity": expected_plan_identity,
        "request_identity": plan["request_identity"],
        "previous_authority_identity": plan["previous_authority_identity"],
        "prospective_authority_identity": plan["prospective_authority_identity"],
        "publication_identity": plan["publication_identity"],
        "publication_custody_identity": plan["publication_custody_identity"],
        "deadline_policy_identity": plan["deadline_policy_identity"],
        "application_authority_identity": applied.authority_identity,
        "child_authority": "independent",
        "child_acceptance": "not-qualified",
        "crash_replay": "not-supported",
    }
    return {**result, "result_identity": canonical_identity(result).uri}


def apply_planned_repository_refresh(
    root: Path,
    request: RepositoryRefreshRequest,
    *,
    expected_plan_identity: str,
    acknowledged: bool,
    deadline_policy: RepositoryFetchDeadlinePolicy,
) -> dict[str, Any]:
    _require_plan_identity(expected_plan_identity)
    prepared = _prepare_repository_refresh_plan(
        root, request, deadline_policy=deadline_policy
    )
    plan = prepared.document
    if plan["plan_identity"] != expected_plan_identity:
        _fail("plan_stale", "reviewed refresh plan no longer matches current inputs")
    if not plan["apply_supported"]:
        _fail(
            "application_platform_unsupported",
            "reviewed refresh selection is unsupported on this platform",
        )
    if acknowledged is not True:
        _fail(
            "acknowledgement_required",
            "repository refresh apply requires explicit acknowledgement",
        )
    publication = prepared.publication
    with reserve_published_refresh(
        publication,
        expected_custody_identity=plan["publication_custody_identity"],
        acknowledge=True,
    ) as owned:
        files = owned.prepare_filesystem_changes()
        objects = files.prepare_objects()
        with objects.stage(metadata=True) as stage:
            applied = apply_repository_refresh(stage)
    return _refresh_apply_result(plan, expected_plan_identity, applied)


__all__ = [
    "APPLY_SCHEMA",
    "CHECK_SCHEMA",
    "PLAN_SCHEMA",
    "PreparedRepositoryRefreshPlan",
    "apply_planned_repository_refresh",
    "check_repository_refresh",
    "load_repository_refresh_request",
    "plan_repository_refresh",
]
