"""Survey open reviews and leftover local Git work at the start or end of a cycle."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.project_tracker import (
    ProjectTrackerError,
    _git_environment,
    inspect_project_tracker,
)
from literate_ai.application.project_tracker import classify_release_relevance
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import discover_project

PEER_WORK_SCHEMA = "literate-ai/project-peer-work@1"
BRANCH_LIFECYCLE_SCHEMA = "literate-ai/branch-lifecycle@1"
BRANCH_LIFECYCLE_PREFIX = "refs/literate-ai/branch-lifecycle/"
_GIT_TIMEOUT_SECONDS = 30
_TRACKER_TIMEOUT_SECONDS = 120

When = Literal["start", "end"]
CommandRunner = Callable[[tuple[str, ...], Path, int], subprocess.CompletedProcess[str]]
Clock = Callable[[], datetime]
_GIT_OBJECT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SAFE_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}")
_MAX_ACTOR_LENGTH = 256
_MAX_REASON_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class BranchLifecycleMarker:
    state: Literal["merged", "dead"]
    branch: str
    head: str
    observed_against: str
    revision: str
    timestamp: datetime
    actor: str
    reason: str | None
    ref: str
    object_name: str

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": BRANCH_LIFECYCLE_SCHEMA,
            "state": self.state,
            "branch": self.branch,
            "head": self.head,
            "observed_against": self.observed_against,
            "revision": self.revision,
            "timestamp": self.timestamp.isoformat(),
            "actor": self.actor,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


class PeerWorkError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def survey_peer_work(
    path: Path,
    *,
    when: str,
    run_command: CommandRunner | None = None,
) -> dict[str, Any]:
    """List green reviews at cycle start, or leftover worktrees at cycle end."""

    if when not in {"start", "end"}:
        raise PeerWorkError(
            "project.peer_work_when_invalid",
            "peer-work --when must be start or end",
        )
    try:
        inspect = inspect_project_tracker(path)
    except ProjectTrackerError as exc:
        raise PeerWorkError(exc.code, exc.message) from exc
    root = Path(str(inspect["git_root"]))
    current = _current_checkout(root)
    default_branch = _default_branch(root)
    worktrees = _list_worktrees(root)
    branches = _list_local_branches(root)
    runner = run_command or _run_text
    project = discover_project(path)
    policy = (
        getattr(project.definition, "repository_policy", None)
        if project is not None
        else None
    )
    reviews = _list_reviews(root, inspect, runner, repository_policy=policy)
    issues = _list_issues(root, inspect, runner) if when == "start" else ()
    local_heads = {item["name"] for item in branches}
    release_branches = _release_branches(root)
    merged_terminal = tuple(
        item
        for item in branches
        if item["name"] not in {current.get("branch"), default_branch}
        and item["merged_into_default"]
    )
    collectable_branches = tuple(
        item
        for item in branches
        if item["name"] not in {current.get("branch"), default_branch}
        and item["name"] not in release_branches
        and not item["merged_into_default"]
    )
    green = tuple(
        item for item in reviews if item["ci"] == "green" and not item["draft"]
    )
    end_reviews = tuple(item for item in reviews if item["head"] in local_heads)
    result: dict[str, Any] = {
        "schema": PEER_WORK_SCHEMA,
        "when": when,
        "forge": inspect["forge"],
        "cli": inspect["cli"],
        "current": current,
        "default_branch": default_branch,
        "reviews": list(reviews if when == "start" else end_reviews),
        "green_reviews": list(green if when == "start" else ()),
        "issues": [dict(item) for item in issues],
        "worktrees": [dict(item) for item in worktrees],
        "collectable_worktrees": [],
        "collectable_branches": (
            [dict(item) for item in collectable_branches] if when == "end" else []
        ),
        "merged_terminal_candidates": (
            [dict(item) for item in merged_terminal] if when == "end" else []
        ),
        "review_status": list(inspect.get("review_status") or ()),
        "issue_status": list(inspect.get("issue_status") or ()),
    }
    result["identity"] = canonical_identity(result).uri
    return result


def classify_github_review(
    payload: Mapping[str, Any], repository_policy: object | None = None
) -> dict[str, Any]:
    """Classify one `gh pr list --json` object. Unknown CI is not green."""

    rollup = payload.get("statusCheckRollup")
    states: list[str] = []
    if isinstance(rollup, list):
        for item in rollup:
            if not isinstance(item, dict):
                states.append("unknown")
                continue
            # CheckRun.status tracks unfinished work while conclusion is empty.
            # Do not drop those checks, or a single completed job masks the rest.
            status = item.get("status")
            state = (
                status
                if status is not None and status != "COMPLETED"
                else item.get("state") or item.get("conclusion")
            )
            states.append(
                state.casefold() if isinstance(state, str) and state else "unknown"
            )
    ci = "unknown"
    if states:
        if any(
            state
            in {
                "failure",
                "error",
                "cancelled",
                "timed_out",
                "action_required",
                "startup_failure",
                "stale",
            }
            for state in states
        ):
            ci = "red"
        elif any(
            state
            in {"pending", "expected", "queued", "in_progress", "waiting", "requested"}
            for state in states
        ):
            ci = "pending"
        elif all(state in {"success", "neutral", "skipped"} for state in states):
            ci = "green"
    head = payload.get("headRefName")
    number = payload.get("number")
    relevance, release = classify_release_relevance(
        payload.get("body"), repository_policy
    )
    labels = payload.get("labels")
    author = payload.get("author")
    return {
        "number": number if isinstance(number, int) else None,
        "title": payload.get("title") if isinstance(payload.get("title"), str) else "",
        "head": head if isinstance(head, str) else "",
        "head_revision": payload.get("headRefOid")
        if isinstance(payload.get("headRefOid"), str)
        else "",
        "base": payload.get("baseRefName")
        if isinstance(payload.get("baseRefName"), str)
        else "",
        "labels": [
            item.get("name")
            for item in labels
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        if isinstance(labels, list)
        else [],
        "author": author.get("login")
        if isinstance(author, dict) and isinstance(author.get("login"), str)
        else "",
        "updated_at": payload.get("updatedAt")
        if isinstance(payload.get("updatedAt"), str)
        else "",
        "release": release,
        "release_relevance": relevance,
        "url": payload.get("url") if isinstance(payload.get("url"), str) else "",
        "draft": bool(payload.get("isDraft")),
        "mergeable": payload.get("mergeable")
        if isinstance(payload.get("mergeable"), str)
        else None,
        "ci": ci,
    }


def classify_github_issue(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one `gh issue list --json` object."""

    number = payload.get("number")
    return {
        "number": number if isinstance(number, int) else None,
        "title": payload.get("title") if isinstance(payload.get("title"), str) else "",
        "url": payload.get("url") if isinstance(payload.get("url"), str) else "",
    }


def _list_reviews(
    root: Path,
    inspect: dict[str, Any],
    runner: CommandRunner,
    *,
    repository_policy: object | None = None,
) -> tuple[dict[str, Any], ...]:
    argv = inspect.get("review_status")
    if not isinstance(argv, list) or not argv:
        return ()
    command = tuple(str(item) for item in argv if isinstance(item, str) and item)
    if not command:
        return ()
    try:
        completed = runner(command, root, _TRACKER_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PeerWorkError(
            "project.peer_work_tracker_unavailable",
            "review status requires the tracker CLI named by inspect",
        ) from exc
    if completed.returncode:
        raise PeerWorkError(
            "project.peer_work_tracker_failed",
            "tracker review-status command failed",
        )
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PeerWorkError(
            "project.peer_work_reviews_invalid",
            "tracker review-status JSON is invalid",
        ) from exc
    if not isinstance(payload, list):
        raise PeerWorkError(
            "project.peer_work_reviews_invalid",
            "tracker review-status JSON must be an array",
        )
    reviews = []
    for item in payload:
        if isinstance(item, dict):
            reviews.append(classify_github_review(item, repository_policy))
    reviews.sort(
        key=lambda item: (
            item["ci"] != "green",
            item["draft"],
            item["number"] or 0,
        )
    )
    return tuple(reviews)


def _list_issues(
    root: Path,
    inspect: dict[str, Any],
    runner: CommandRunner,
) -> tuple[dict[str, Any], ...]:
    argv = inspect.get("issue_status")
    if not isinstance(argv, list) or not argv:
        return ()
    command = tuple(str(item) for item in argv if isinstance(item, str) and item)
    if not command:
        return ()
    try:
        completed = runner(command, root, _TRACKER_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PeerWorkError(
            "project.peer_work_tracker_unavailable",
            "issue status requires the tracker CLI named by inspect",
        ) from exc
    if completed.returncode:
        raise PeerWorkError(
            "project.peer_work_tracker_failed",
            "tracker issue-status command failed",
        )
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PeerWorkError(
            "project.peer_work_issues_invalid",
            "tracker issue-status JSON is invalid",
        ) from exc
    if not isinstance(payload, list):
        raise PeerWorkError(
            "project.peer_work_issues_invalid",
            "tracker issue-status JSON must be an array",
        )
    issues = []
    for item in payload:
        if isinstance(item, dict):
            issues.append(classify_github_issue(item))
    issues.sort(key=lambda item: item["number"] or 0)
    return tuple(issues)


def _current_checkout(root: Path) -> dict[str, str]:
    branch = _git_text(root, "rev-parse", "--abbrev-ref", "HEAD")
    revision = _git_text(root, "rev-parse", "HEAD")
    return {
        "path": str(root.resolve()),
        "branch": branch if branch != "HEAD" else "",
        "revision": revision,
    }


def _default_branch(root: Path) -> str:
    repository_policy = _repository_policy(root)
    configured = getattr(repository_policy, "default_branch", None)
    if isinstance(configured, str) and configured:
        return configured
    remote = _policy_remote(repository_policy)
    completed = _git(root, "symbolic-ref", f"refs/remotes/{remote}/HEAD")
    if completed.returncode == 0:
        text = completed.stdout.strip()
        prefix = f"refs/remotes/{remote}/"
        if text.startswith(prefix) and text[len(prefix) :]:
            return text[len(prefix) :]
    policy = root / "literate.release.json"
    if policy.is_file():
        try:
            data = json.loads(policy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            named = data.get("default_branch")
            if isinstance(named, str) and named:
                return named
    return "main"


def _list_worktrees(root: Path) -> tuple[dict[str, str], ...]:
    completed = _git(root, "worktree", "list", "--porcelain")
    if completed.returncode:
        raise PeerWorkError(
            "project.peer_work_git_failed",
            "git worktree list failed",
        )
    items: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if not line:
            if current.get("path"):
                items.append(current)
            current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = str(Path(line[len("worktree ") :]).resolve())
        elif line.startswith("HEAD "):
            current["revision"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            prefix = "refs/heads/"
            current["branch"] = ref[len(prefix) :] if ref.startswith(prefix) else ref
        elif line == "detached":
            current["branch"] = ""
    if current.get("path"):
        items.append(current)
    for item in items:
        status = _git_at(Path(item["path"]), "status", "--porcelain")
        item["status"] = (
            "clean" if status.returncode == 0 and not status.stdout else "dirty"
        )
    items.sort(key=lambda item: item["path"])
    return tuple(items)


def _list_local_branches(root: Path) -> tuple[dict[str, Any], ...]:
    completed = _git(
        root,
        "for-each-ref",
        "--format=%(refname:short)%09%(objectname)",
        "refs/heads",
    )
    if completed.returncode:
        raise PeerWorkError(
            "project.peer_work_git_failed",
            "git for-each-ref failed",
        )
    default_revision = _git_text(root, "rev-parse", _default_branch(root))
    current_revision = _git_text(root, "rev-parse", "HEAD")
    items: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        name, separator, revision = line.partition("\t")
        if not separator or not name:
            continue
        merged = _git(root, "merge-base", "--is-ancestor", revision, default_revision)
        merged_current = _git(
            root, "merge-base", "--is-ancestor", revision, current_revision
        )
        items.append(
            {
                "name": name,
                "revision": revision,
                "merged_into_default": merged.returncode == 0,
                "merged_into_current": merged_current.returncode == 0,
            }
        )
    items.sort(key=lambda item: str(item["name"]))
    return tuple(items)


def mark_branch_lifecycle(
    path: Path,
    *,
    state: str,
    branch: str,
    actor: str,
    reason: str | None = None,
    push: bool = False,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Persist an exact branch-head lifecycle observation under a fetchable Git ref."""

    if state not in {"merged", "dead"}:
        raise PeerWorkError(
            "project.peer_work_state_invalid", "state must be merged or dead"
        )
    if not _safe_branch(branch):
        raise PeerWorkError(
            "project.peer_work_branch_invalid", "branch must be a safe Git branch name"
        )
    if not actor or actor.strip() != actor or len(actor) > _MAX_ACTOR_LENGTH:
        raise PeerWorkError(
            "project.peer_work_actor_invalid", "actor must be a bounded non-empty value"
        )
    if reason is not None and (
        not reason or reason.strip() != reason or len(reason) > _MAX_REASON_LENGTH
    ):
        raise PeerWorkError(
            "project.peer_work_reason_invalid",
            "reason must be a bounded non-empty value",
        )
    if state == "dead" and reason is None:
        raise PeerWorkError(
            "project.peer_work_reason_required", "dead markers require a reason"
        )
    root = _peer_work_root(path)
    repository_policy = _repository_policy(root)
    remote = _policy_remote(repository_policy)
    local_head = _git(root, "rev-parse", "--verify", f"refs/heads/{branch}")
    if local_head.returncode == 0:
        revision = local_head.stdout.strip()
    else:
        remote_head = _git(
            root, "rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"
        )
        if remote_head.returncode:
            raise PeerWorkError(
                "project.peer_work_branch_unknown",
                "branch lifecycle marker requires a local or fetched remote branch",
            )
        revision = remote_head.stdout.strip()
    default = _default_branch(root)
    observed_revision = _git_text(root, "rev-parse", default)
    if (
        state == "merged"
        and _git(
            root, "merge-base", "--is-ancestor", revision, observed_revision
        ).returncode
    ):
        raise PeerWorkError(
            "project.peer_work_not_merged",
            "a merged marker requires the exact branch head to be in the "
            "default branch",
        )
    record: dict[str, Any] = {
        "schema": BRANCH_LIFECYCLE_SCHEMA,
        "state": state,
        "branch": branch,
        "head": revision,
        "observed_against": default,
        "revision": observed_revision,
        "timestamp": _now(clock).isoformat(),
        "actor": actor,
    }
    if reason:
        record["reason"] = reason
    ref = _marker_ref(branch)
    blob = _git_input(
        root,
        json.dumps(record, sort_keys=True).encode(),
        "hash-object",
        "-w",
        "--stdin",
    )
    if blob.returncode:
        raise PeerWorkError(
            "project.peer_work_git_failed", "could not write lifecycle marker"
        )
    if _git(root, "update-ref", ref, blob.stdout.strip()).returncode:
        raise PeerWorkError(
            "project.peer_work_git_failed", "could not update lifecycle marker ref"
        )
    if push and _git(root, "push", remote, f"{ref}:{ref}").returncode:
        raise PeerWorkError(
            "project.peer_work_push_failed", "could not push lifecycle marker ref"
        )
    return {**record, "ref": ref, "object": blob.stdout.strip(), "pushed": push}


def garbage_collect_peer_work(
    path: Path,
    *,
    apply: bool = False,
    authorize_delete: bool = False,
    run_command: CommandRunner | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Plan or safely remove local branches whose durable markers still match."""

    if apply and not authorize_delete:
        raise PeerWorkError(
            "project.peer_work_delete_not_authorized",
            "--apply requires --authorize-delete",
        )
    try:
        inspect = inspect_project_tracker(path)
    except ProjectTrackerError as exc:
        raise PeerWorkError(exc.code, exc.message) from exc
    root = Path(str(inspect["git_root"]))
    if apply and (inspect.get("forge") != "github" or not inspect.get("review_status")):
        raise PeerWorkError(
            "project.peer_work_review_status_required",
            "destructive GC requires a supported forge and open-review query",
        )
    repository_policy = _repository_policy(root)
    now = _now(clock)
    minimum_age_days = _minimum_age_days(repository_policy)
    current = _current_checkout(root)
    default = _default_branch(root)
    worktrees = _list_worktrees(root)
    reviews = _list_reviews(root, inspect, run_command or _run_text)
    open_heads = {item["head"] for item in reviews}
    release_branches = _release_branches(root)
    markers, invalid_markers = _read_markers(root)
    repository_dirty = any(item.get("status") != "clean" for item in worktrees)
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = list(invalid_markers)
    for marker in markers:
        branch = marker.branch
        head = marker.head
        reasons: list[str] = []
        actual = _git(root, "rev-parse", "--verify", f"refs/heads/{branch}")
        if actual.returncode or actual.stdout.strip() != head:
            reasons.append("stale-marker")
        if branch in open_heads:
            reasons.append("open-review")
        if branch in {current.get("branch"), default} or branch in release_branches:
            reasons.append("protected-branch")
        if (
            not actual.returncode
            and _git(root, "merge-base", "--is-ancestor", str(head), default).returncode
        ):
            reasons.append("branch-not-merged")
        attached = [item for item in worktrees if item.get("branch") == branch]
        if any(item.get("status") != "clean" for item in attached):
            reasons.append("dirty-worktree")
        if repository_dirty and "dirty-worktree" not in reasons:
            reasons.append("dirty-state")
        age = now - marker.timestamp
        if age.total_seconds() < 0:
            reasons.append("future-marker")
        elif age.total_seconds() < minimum_age_days * 86400:
            reasons.append("marker-too-young")
        item = {
            "branch": branch,
            "head": head,
            "state": marker.state,
            "ref": marker.ref,
            "object": marker.object_name,
            "worktrees": attached,
        }
        (rejected if reasons else candidates).append(
            {**item, **({"reasons": reasons} if reasons else {})}
        )
    deleted: list[str] = []
    if apply:
        for candidate in candidates:
            reasons = _gc_revalidation_reasons(
                root,
                inspect,
                candidate,
                run_command or _run_text,
                now=_now(clock),
                minimum_age_days=minimum_age_days,
            )
            if reasons:
                rejected.append({**candidate, "reasons": reasons})
                continue
            for worktree in candidate["worktrees"]:
                if Path(worktree["path"]).resolve() != Path(current["path"]).resolve():
                    if _git(root, "worktree", "remove", worktree["path"]).returncode:
                        raise PeerWorkError(
                            "project.peer_work_gc_failed",
                            "clean worktree removal failed",
                        )
            _git(root, "worktree", "prune")
            reasons = _gc_revalidation_reasons(
                root,
                inspect,
                candidate,
                run_command or _run_text,
                now=_now(clock),
                minimum_age_days=minimum_age_days,
            )
            if reasons:
                rejected.append({**candidate, "reasons": reasons})
                continue
            if _git(
                root,
                "update-ref",
                "-d",
                f"refs/heads/{candidate['branch']}",
                candidate["head"],
            ).returncode:
                rejected.append({**candidate, "reasons": ["stale-branch"]})
            else:
                deleted.append(candidate["branch"])
    return {
        "schema": PEER_WORK_SCHEMA,
        "dry_run": not apply,
        "candidates": candidates,
        "rejected": rejected,
        "deleted": deleted,
    }


def _marker_ref(branch: str) -> str:
    return BRANCH_LIFECYCLE_PREFIX + hashlib.sha256(branch.encode()).hexdigest()


def _peer_work_root(path: Path) -> Path:
    try:
        inspect = inspect_project_tracker(path)
    except ProjectTrackerError as exc:
        raise PeerWorkError(exc.code, exc.message) from exc
    return Path(str(inspect["git_root"]))


def _read_markers(
    root: Path,
    *,
    prefixes: tuple[str, ...] = (BRANCH_LIFECYCLE_PREFIX,),
) -> tuple[list[BranchLifecycleMarker], list[dict[str, Any]]]:
    records: list[BranchLifecycleMarker] = []
    invalid: list[dict[str, Any]] = []
    for prefix in prefixes:
        refs = _git(
            root,
            "for-each-ref",
            "--format=%(refname)%09%(objectname)",
            prefix,
        )
        for line in refs.stdout.splitlines():
            ref, separator, object_name = line.partition("\t")
            if not separator:
                continue
            value = _git(root, "cat-file", "blob", object_name)
            try:
                record = json.loads(value.stdout)
                records.append(
                    _parse_marker(
                        record,
                        ref=ref,
                        binding_ref=BRANCH_LIFECYCLE_PREFIX + ref.removeprefix(prefix),
                        object_name=object_name,
                    )
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                invalid.append(
                    {
                        "ref": ref,
                        "object": object_name,
                        "reasons": ["invalid-marker"],
                    }
                )
    return records, invalid


def load_branch_lifecycle_markers(
    root: Path,
    *,
    additional_prefixes: tuple[str, ...] = (),
) -> tuple[tuple[BranchLifecycleMarker, ...], tuple[dict[str, Any], ...]]:
    """Read exact-head branch dispositions for release and GC consumers."""

    records, invalid = _read_markers(
        Path(root), prefixes=(BRANCH_LIFECYCLE_PREFIX, *additional_prefixes)
    )
    return tuple(records), tuple(invalid)


def _parse_marker(
    value: object,
    *,
    ref: str,
    object_name: str,
    binding_ref: str | None = None,
) -> BranchLifecycleMarker:
    if _GIT_OBJECT.fullmatch(object_name) is None:
        raise ValueError("invalid marker object")
    required_fields = {
        "schema",
        "state",
        "branch",
        "head",
        "observed_against",
        "revision",
        "timestamp",
        "actor",
    }
    optional_fields = {
        "schema",
        "state",
        "branch",
        "head",
        "observed_against",
        "revision",
        "timestamp",
        "actor",
        "reason",
    }
    if not isinstance(value, dict) or set(value) not in (
        required_fields,
        optional_fields,
    ):
        raise ValueError("invalid marker fields")
    state = value.get("state")
    branch = value.get("branch")
    head = value.get("head")
    observed_against = value.get("observed_against")
    revision = value.get("revision")
    actor = value.get("actor")
    reason = value.get("reason")
    if value.get("schema") != BRANCH_LIFECYCLE_SCHEMA or state not in {
        "merged",
        "dead",
    }:
        raise ValueError("invalid marker schema or state")
    if not isinstance(branch, str) or not _safe_branch(branch):
        raise ValueError("invalid branch")
    if (binding_ref or ref) != _marker_ref(branch):
        raise ValueError("marker ref does not bind branch")
    if not isinstance(head, str) or _GIT_OBJECT.fullmatch(head) is None:
        raise ValueError("invalid head")
    if not isinstance(revision, str) or _GIT_OBJECT.fullmatch(revision) is None:
        raise ValueError("invalid observed revision")
    if not isinstance(observed_against, str) or not _safe_branch(observed_against):
        raise ValueError("invalid observed branch")
    if (
        not isinstance(actor, str)
        or not actor
        or actor.strip() != actor
        or len(actor) > _MAX_ACTOR_LENGTH
    ):
        raise ValueError("invalid actor")
    if reason is not None and (
        not isinstance(reason, str)
        or not reason
        or reason.strip() != reason
        or len(reason) > _MAX_REASON_LENGTH
    ):
        raise ValueError("invalid reason")
    if state == "dead" and reason is None:
        raise ValueError("dead marker requires reason")
    timestamp = value.get("timestamp")
    if not isinstance(timestamp, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must have a timezone")
    if parsed.isoformat() != timestamp:
        raise ValueError("timestamp is not canonical")
    return BranchLifecycleMarker(
        state,
        branch,
        head,
        observed_against,
        revision,
        parsed.astimezone(UTC),
        actor,
        reason,
        ref,
        object_name,
    )


def _gc_revalidation_reasons(
    root: Path,
    inspect: Mapping[str, Any],
    candidate: Mapping[str, Any],
    runner: CommandRunner,
    *,
    now: datetime,
    minimum_age_days: int,
) -> list[str]:
    markers, invalid = _read_markers(root)
    marker = next(
        (
            item
            for item in markers
            if item.ref == candidate["ref"] and item.object_name == candidate["object"]
        ),
        None,
    )
    if marker is None or invalid:
        return ["stale-marker"]
    reasons: list[str] = []
    branch = marker.branch
    default = _default_branch(root)
    current = _current_checkout(root)
    reviews = _list_reviews(root, dict(inspect), runner)
    if branch in {item["head"] for item in reviews}:
        reasons.append("open-review")
    actual = _git(root, "rev-parse", "--verify", f"refs/heads/{branch}")
    if actual.returncode or actual.stdout.strip() != marker.head:
        reasons.append("stale-marker")
    if branch in {current.get("branch"), default} or branch in _release_branches(root):
        reasons.append("protected-branch")
    if _git(root, "merge-base", "--is-ancestor", marker.head, default).returncode:
        reasons.append("branch-not-merged")
    worktrees = _list_worktrees(root)
    if any(item.get("status") != "clean" for item in worktrees):
        reasons.append("dirty-state")
    age = now - marker.timestamp
    if age.total_seconds() < 0:
        reasons.append("future-marker")
    elif age.total_seconds() < minimum_age_days * 86400:
        reasons.append("marker-too-young")
    return reasons


def _safe_branch(branch: str) -> bool:
    return bool(
        _SAFE_BRANCH.fullmatch(branch)
        and ".." not in branch
        and "//" not in branch
        and "@{" not in branch
        and not branch.endswith(("/", ".", ".lock"))
    )


def _repository_policy(root: Path) -> object | None:
    project = discover_project(root)
    return (
        getattr(project.definition, "repository_policy", None)
        if project is not None
        else None
    )


def _policy_remote(policy: object | None) -> str:
    remote = getattr(policy, "remote", None)
    return remote if isinstance(remote, str) and remote else "origin"


def _minimum_age_days(policy: object | None) -> int:
    days = getattr(policy, "branch_gc_minimum_age_days", None)
    return (
        days
        if isinstance(days, int) and not isinstance(days, bool) and days >= 1
        else 7
    )


def _now(clock: Clock | None) -> datetime:
    value = (clock or (lambda: datetime.now(UTC)))()
    if value.tzinfo is None or value.utcoffset() is None:
        raise PeerWorkError(
            "project.peer_work_clock_invalid",
            "peer-work clock must return aware UTC time",
        )
    return value.astimezone(UTC)


def _release_branches(root: Path) -> set[str]:
    policy = root / "literate.release.json"
    try:
        value = json.loads(policy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    names = value.get("release_branches", ()) if isinstance(value, dict) else ()
    explicit = {item for item in names if isinstance(item, str)}
    branches = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    explicit.update(
        branch
        for branch in branches.stdout.splitlines()
        if branch.startswith("release/")
    )
    return explicit


def _git_at(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _git(root, *arguments)


def _git_input(
    root: Path, data: bytes, *arguments: str
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        input=data,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode(errors="replace"),
        result.stderr.decode(errors="replace"),
    )


def _git_text(root: Path, *arguments: str) -> str:
    completed = _git(root, *arguments)
    if completed.returncode:
        raise PeerWorkError(
            "project.peer_work_git_failed",
            f"git {' '.join(arguments)} failed",
        )
    return completed.stdout.strip()


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        result = run_with_tree_kill(
            ("git", "-C", str(root), *arguments),
            env=_git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PeerWorkError(
            "project.peer_work_git_unavailable",
            "git is required to survey worktrees and branches",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PeerWorkError(
            "project.peer_work_git_timeout",
            "git did not finish before the peer-work deadline",
        ) from exc
    stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
    return subprocess.CompletedProcess(result.args, result.returncode, stdout, stderr)


def _run_text(
    command: tuple[str, ...], root: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    result = run_with_tree_kill(
        command,
        env=_git_environment(),
        timeout=timeout,
        check=False,
        cwd=root,
    )
    stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
    return subprocess.CompletedProcess(result.args, result.returncode, stdout, stderr)


__all__ = [
    "PEER_WORK_SCHEMA",
    "BRANCH_LIFECYCLE_SCHEMA",
    "PeerWorkError",
    "classify_github_issue",
    "classify_github_review",
    "garbage_collect_peer_work",
    "load_branch_lifecycle_markers",
    "mark_branch_lifecycle",
    "survey_peer_work",
]
