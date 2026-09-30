"""Inventory and disposition release-relevant tracker and Git contributions."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from literate_ai.adapters.peer_work import (
    BranchLifecycleMarker,
    load_branch_lifecycle_markers,
)
from literate_ai.adapters.project_tracker import (
    ProjectTrackerError,
    _git_environment,
    inspect_project_tracker,
)
from literate_ai.contracts.identity import canonical_identity

RELEASE_CONTRIBUTIONS_SCHEMA = "literate-ai/release-contributions@1"
RELEASE_DISPOSITION_SCHEMA = "literate-ai/release-disposition@1"
RELEASE_DISPOSITION_MARKER = "literate-ai-release-disposition:v1"

_TRACKER_TIMEOUT_SECONDS = 120
_GIT_TIMEOUT_SECONDS = 30
_RELEASE_BRANCH = re.compile(r"release/[0-9]+\.[0-9]+\.x")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MARKER = re.compile(
    rf"<!--\s*{re.escape(RELEASE_DISPOSITION_MARKER)}\s*\n"
    r"(?P<payload>\{[^\n]+\})\s*\n-->",
)

CommandRunner = Callable[[tuple[str, ...], Path, int], subprocess.CompletedProcess[str]]


class ReleaseContributionsError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _run(
    command: tuple[str, ...], root: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=root,
        env=_git_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _completed_json(
    runner: CommandRunner,
    command: tuple[str, ...],
    root: Path,
    *,
    code: str,
) -> object:
    try:
        completed = runner(command, root, _TRACKER_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseContributionsError(
            "release.contributions_tracker_unavailable",
            f"release contribution inventory requires {' '.join(command[:2])}",
        ) from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[:512]
        raise ReleaseContributionsError(
            "release.contributions_tracker_failed",
            f"tracker contribution query failed: {detail or code}",
        )
    try:
        return json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ReleaseContributionsError(code, "tracker returned invalid JSON") from exc


def _repository_slug(url: object) -> str:
    if not isinstance(url, str) or not url:
        raise ReleaseContributionsError(
            "release.contributions_repository_unknown",
            "tracker inspection did not identify a repository URL",
        )
    value = url.rstrip("/")
    if "://" in value:
        path = urllib.parse.urlparse(value).path
    else:
        _host, separator, path = value.partition(":")
        if not separator:
            path = value
    slug = path.strip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    if len(slug.split("/")) < 2:
        raise ReleaseContributionsError(
            "release.contributions_repository_unknown",
            "tracker repository URL does not contain an owner and repository",
        )
    return slug


def _comment_bodies(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            body = item.get("body") or item.get("bodyText")
            if isinstance(body, str):
                result.append(body)
    return tuple(result)


def parse_release_disposition(body: str, *, release: str) -> dict[str, object] | None:
    """Return the last valid disposition for ``release`` in one comment body."""

    selected: dict[str, object] | None = None
    for match in _MARKER.finditer(body):
        try:
            payload = json.loads(match.group("payload"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if set(payload) != {
            "schema",
            "release",
            "decision",
            "milestone",
            "branches",
        }:
            continue
        branches = payload.get("branches")
        if (
            payload.get("schema") != RELEASE_DISPOSITION_SCHEMA
            or payload.get("release") != release
            or payload.get("decision") not in {"include", "defer"}
            or not isinstance(payload.get("milestone"), str)
            or not payload["milestone"]
            or not isinstance(branches, list)
            or any(not isinstance(item, str) or not item for item in branches)
        ):
            continue
        selected = payload
    return selected


def render_release_disposition(
    *,
    release: str,
    decision: str,
    milestone: str,
    reason: str,
    branches: Sequence[str] = (),
) -> str:
    if _VERSION.fullmatch(release) is None:
        raise ReleaseContributionsError(
            "release.contributions_version_invalid",
            "release must be a canonical three-part version",
        )
    if decision not in {"include", "defer"}:
        raise ReleaseContributionsError(
            "release.contributions_decision_invalid",
            "decision must be include or defer",
        )
    if not milestone.strip() or milestone.strip() != milestone:
        raise ReleaseContributionsError(
            "release.contributions_milestone_invalid",
            "milestone must be a non-empty trimmed title",
        )
    if not reason.strip() or reason.strip() != reason:
        raise ReleaseContributionsError(
            "release.contributions_reason_invalid",
            "reason must be a non-empty trimmed explanation",
        )
    normalized_branches = sorted(set(branches))
    if any(not branch or branch.strip() != branch for branch in normalized_branches):
        raise ReleaseContributionsError(
            "release.contributions_branch_invalid",
            "branch names must be non-empty and trimmed",
        )
    payload = {
        "schema": RELEASE_DISPOSITION_SCHEMA,
        "release": release,
        "decision": decision,
        "milestone": milestone,
        "branches": normalized_branches,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    action = "included in" if decision == "include" else "deferred from"
    return (
        f"<!-- {RELEASE_DISPOSITION_MARKER}\n{encoded}\n-->\n"
        f"Release engineering: {action} {release}; milestone `{milestone}`.\n\n"
        f"Reason: {reason}"
    )


def _milestone_title(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    title = value.get("title")
    return title if isinstance(title, str) and title else None


def _classify_item(
    item: Mapping[str, Any],
    *,
    kind: str,
    release: str,
    current_milestone: str,
) -> dict[str, object]:
    comments = _comment_bodies(item.get("comments"))
    disposition = None
    for comment in comments:
        candidate = parse_release_disposition(comment, release=release)
        if candidate is not None:
            disposition = candidate
    milestone = _milestone_title(item.get("milestone"))
    reasons: list[str] = []
    decision = disposition.get("decision") if disposition else None
    expected = disposition.get("milestone") if disposition else None
    if disposition is None:
        reasons.append("missing-current-release-disposition")
    elif milestone != expected:
        reasons.append("disposition-milestone-mismatch")
    elif decision == "include" and milestone != current_milestone:
        reasons.append("included-item-not-in-current-milestone")
    elif decision == "defer" and milestone == current_milestone:
        reasons.append("deferred-item-still-in-current-milestone")
    elif decision == "defer" and milestone is None:
        reasons.append("deferred-item-has-no-milestone")
    number = item.get("number")
    if not isinstance(number, int):
        number = item.get("iid")
    return {
        "kind": kind,
        "number": number if isinstance(number, int) else None,
        "title": str(item.get("title") or ""),
        "url": str(item.get("url") or item.get("web_url") or ""),
        "updated_at": str(item.get("updatedAt") or item.get("updated_at") or ""),
        "milestone": milestone,
        "decision": decision,
        "disposition": disposition,
        "reasons": reasons,
    }


def _github_items(
    root: Path, slug: str, runner: CommandRunner
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    common = ("number", "title", "url", "updatedAt", "milestone", "comments")
    issues = _completed_json(
        runner,
        (
            "gh",
            "issue",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            ",".join(common),
        ),
        root,
        code="release.contributions_issues_invalid",
    )
    reviews = _completed_json(
        runner,
        (
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            ",".join(
                (
                    *common,
                    "headRefName",
                    "headRefOid",
                    "baseRefName",
                    "isDraft",
                    "mergeable",
                    "author",
                    "statusCheckRollup",
                )
            ),
        ),
        root,
        code="release.contributions_reviews_invalid",
    )
    if not isinstance(issues, list) or not isinstance(reviews, list):
        raise ReleaseContributionsError(
            "release.contributions_tracker_invalid",
            "tracker issue and review queries must return arrays",
        )
    return (
        [dict(item) for item in issues if isinstance(item, Mapping)],
        [dict(item) for item in reviews if isinstance(item, Mapping)],
    )


def _gitlab_notes(
    root: Path,
    project: str,
    resource: str,
    iid: int,
    runner: CommandRunner,
) -> list[dict[str, object]]:
    # Classification consumes oldest first, with the last valid marker winning.
    # GitLab defaults to newest first. Request a stable ascending traversal and
    # normalize by immutable creation ID, including notes beyond the first page.
    notes: dict[int, dict[str, object]] = {}
    page = 1
    while True:
        value = _completed_json(
            runner,
            (
                "glab",
                "api",
                f"projects/{project}/{resource}/{iid}/notes?per_page=100"
                f"&order_by=created_at&sort=asc&page={page}",
            ),
            root,
            code="release.contributions_comments_invalid",
        )
        if not isinstance(value, list):
            raise ReleaseContributionsError(
                "release.contributions_comments_invalid",
                "GitLab notes query must return an array",
            )
        for item in value:
            note_id = item.get("id") if isinstance(item, Mapping) else None
            if type(note_id) is not int or note_id <= 0 or note_id in notes:
                raise ReleaseContributionsError(
                    "release.contributions_comments_invalid",
                    "GitLab notes must have unique positive integer IDs across pages",
                )
            notes[note_id] = dict(item)
        if len(value) < 100:
            return [notes[note_id] for note_id in sorted(notes)]
        page += 1


def _gitlab_items(
    root: Path, slug: str, runner: CommandRunner
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    project = urllib.parse.quote(slug, safe="")
    issue_value = _completed_json(
        runner,
        ("glab", "api", f"projects/{project}/issues?state=opened&per_page=100"),
        root,
        code="release.contributions_issues_invalid",
    )
    review_value = _completed_json(
        runner,
        (
            "glab",
            "api",
            f"projects/{project}/merge_requests?state=opened&per_page=100",
        ),
        root,
        code="release.contributions_reviews_invalid",
    )
    if not isinstance(issue_value, list) or not isinstance(review_value, list):
        raise ReleaseContributionsError(
            "release.contributions_tracker_invalid",
            "GitLab issue and merge-request queries must return arrays",
        )
    issues = [dict(item) for item in issue_value if isinstance(item, Mapping)]
    reviews = [dict(item) for item in review_value if isinstance(item, Mapping)]
    for resource, items in (("issues", issues), ("merge_requests", reviews)):
        for item in items:
            iid = item.get("iid")
            if isinstance(iid, int):
                item["comments"] = _gitlab_notes(root, project, resource, iid, runner)
    return issues, reviews


def _git_lines(root: Path, *arguments: str) -> list[str]:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            env=_git_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseContributionsError(
            "release.contributions_git_unavailable",
            "Git contribution inventory is unavailable",
        ) from exc
    if completed.returncode:
        raise ReleaseContributionsError(
            "release.contributions_git_failed",
            f"Git contribution inventory failed: {' '.join(arguments)}",
        )
    return completed.stdout.splitlines()


def _refresh_remote_contribution_refs(root: Path, remote: str) -> str:
    """Fetch current branch and lifecycle refs without touching a checkout."""

    if _REMOTE.fullmatch(remote) is None:
        raise ReleaseContributionsError(
            "release.contributions_remote_invalid",
            "release contribution remote must be a safe Git remote name",
        )
    lifecycle_prefix = f"refs/literate-ai/release-observed/{remote}/branch-lifecycle/"
    _git_lines(
        root,
        "fetch",
        "--prune",
        "--no-tags",
        remote,
        f"+refs/heads/*:refs/remotes/{remote}/*",
        "+refs/literate-ai/branch-lifecycle/*:" + lifecycle_prefix + "*",
    )
    return lifecycle_prefix


def _revision(root: Path, reference: str) -> str:
    lines = _git_lines(root, "rev-parse", "--verify", reference + "^{commit}")
    if (
        len(lines) != 1
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", lines[0]) is None
    ):
        raise ReleaseContributionsError(
            "release.contributions_git_failed", "Git did not return one exact commit"
        )
    return lines[0]


def _unmerged_branches(
    root: Path,
    *,
    remote: str,
    default_branch: str,
    default_revision: str,
    review_heads: set[str],
    disposition_branches: Mapping[str, dict[str, object]],
    lifecycle_markers: Mapping[str, Sequence[BranchLifecycleMarker]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    queries = (
        ("local", "refs/heads"),
        ("remote", f"refs/remotes/{remote}"),
    )
    seen: set[tuple[str, str]] = set()
    for location, namespace in queries:
        lines = _git_lines(
            root,
            "for-each-ref",
            f"--no-merged={default_revision}",
            "--format=%(refname:short)%09%(objectname)",
            namespace,
        )
        for line in lines:
            name, separator, revision = line.partition("\t")
            if not separator:
                continue
            branch = name
            if location == "remote":
                prefix = f"{remote}/"
                if not branch.startswith(prefix):
                    continue
                branch = branch[len(prefix) :]
            if (
                branch in {"HEAD", default_branch}
                or branch.endswith("/HEAD")
                or _RELEASE_BRANCH.fullmatch(branch)
            ):
                continue
            key = (location, branch)
            if key in seen:
                continue
            seen.add(key)
            disposition = disposition_branches.get(branch)
            branch_markers = lifecycle_markers.get(branch, ())
            lifecycle = next(
                (
                    marker
                    for marker in branch_markers
                    if marker.head == revision and marker.state == "dead"
                ),
                branch_markers[-1] if branch_markers else None,
            )
            resolved_by = None
            if branch in review_heads:
                resolved_by = "open-review"
            elif disposition is not None:
                resolved_by = "tracker-disposition"
            elif (
                lifecycle is not None
                and lifecycle.head == revision
                and lifecycle.state == "dead"
            ):
                resolved_by = "branch-lifecycle"
            result.append(
                {
                    "location": location,
                    "name": branch,
                    "revision": revision,
                    "resolved_by": resolved_by,
                    "disposition": disposition,
                    "lifecycle": (
                        None
                        if lifecycle is None
                        else {
                            **lifecycle.to_dict(),
                            "ref": lifecycle.ref,
                            "object": lifecycle.object_name,
                        }
                    ),
                    "reasons": []
                    if resolved_by
                    else ["unmerged-branch-has-no-review-or-disposition"],
                }
            )
    result.sort(key=lambda item: (str(item["name"]), str(item["location"])))
    return result


def _worktrees(
    root: Path, resolved_branches: Mapping[str, str], *, default_branch: str
) -> list[dict[str, object]]:
    lines = _git_lines(root, "worktree", "list", "--porcelain")
    parsed: list[dict[str, object]] = []
    current: dict[str, object] = {}
    for line in (*lines, ""):
        if not line:
            if current.get("path"):
                parsed.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = str(Path(line.removeprefix("worktree ")).resolve())
        elif line.startswith("HEAD "):
            current["revision"] = line.removeprefix("HEAD ")
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line.removeprefix("branch refs/heads/")
        elif line == "detached":
            current["branch"] = None
        elif line.startswith("prunable"):
            current["prunable"] = True
    for item in parsed:
        path = Path(str(item["path"]))
        primary = path == root.resolve()
        prunable = bool(item.get("prunable")) or not path.exists()
        status_lines = [] if prunable else _git_lines(path, "status", "--porcelain")
        dirty = bool(status_lines)
        branch = item.get("branch")
        changed = False
        if not prunable:
            observed_branch = _git_lines(
                path, "rev-parse", "--symbolic-full-name", "HEAD"
            )
            expected_branch = "HEAD" if branch is None else f"refs/heads/{branch}"
            changed = _revision(path, "HEAD") != item.get(
                "revision"
            ) or observed_branch != [expected_branch]
        reasons: list[str] = []
        if prunable:
            reasons.append("attached-worktree-is-prunable")
        elif changed:
            reasons.append("attached-worktree-changed")
        elif not primary and dirty:
            reasons.append("attached-worktree-is-dirty")
        elif (
            not primary
            and isinstance(branch, str)
            and branch != default_branch
            and resolved_branches.get(branch) != item.get("revision")
            and not _RELEASE_BRANCH.fullmatch(branch)
        ):
            reasons.append("attached-worktree-branch-is-unresolved")
        item.update(
            {
                "primary": primary,
                "status": "dirty" if dirty else "clean",
                "reasons": reasons,
            }
        )
    parsed.sort(key=lambda item: str(item["path"]))
    return parsed


def sweep_release_contributions(
    path: Path,
    *,
    release: str,
    current_milestone: str,
    remote: str = "origin",
    default_branch: str = "main",
    run_command: CommandRunner | None = None,
    refresh_remote: bool = True,
) -> dict[str, object]:
    """Return a read-only normalized contribution closure for one release cycle."""

    if _VERSION.fullmatch(release) is None:
        raise ReleaseContributionsError(
            "release.contributions_version_invalid",
            "release must be a canonical three-part version",
        )
    try:
        inspect = inspect_project_tracker(path)
    except ProjectTrackerError as exc:
        raise ReleaseContributionsError(exc.code, exc.message) from exc
    forge = inspect.get("forge")
    if forge not in {"github", "gitlab"}:
        raise ReleaseContributionsError(
            "release.contributions_tracker_unsupported",
            f"release contribution closure requires GitHub or GitLab, not {forge}",
        )
    root = Path(str(inspect["git_root"]))
    slug = _repository_slug(inspect.get("url"))
    remote_marker_prefixes = (
        (_refresh_remote_contribution_refs(root, remote),) if refresh_remote else ()
    )
    runner = run_command or _run
    if forge == "github":
        issue_payloads, review_payloads = _github_items(root, slug, runner)
    else:
        issue_payloads, review_payloads = _gitlab_items(root, slug, runner)
    issues = [
        _classify_item(
            item,
            kind="issue",
            release=release,
            current_milestone=current_milestone,
        )
        for item in issue_payloads
    ]
    reviews = [
        _classify_item(
            item,
            kind="review",
            release=release,
            current_milestone=current_milestone,
        )
        for item in review_payloads
    ]
    disposition_branches: dict[str, dict[str, object]] = {}
    for item in (*issues, *reviews):
        disposition = item.get("disposition")
        if not isinstance(disposition, dict) or item["reasons"]:
            continue
        branches = disposition.get("branches")
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, str):
                    disposition_branches[branch] = {
                        "kind": item["kind"],
                        "number": item["number"],
                        "decision": item["decision"],
                        "milestone": item["milestone"],
                    }
    review_heads = {
        str(item.get("headRefName") or item.get("source_branch") or "")
        for item in review_payloads
    }
    review_heads.discard("")
    markers, invalid_markers = load_branch_lifecycle_markers(
        root, additional_prefixes=remote_marker_prefixes
    )
    lifecycle_markers: dict[str, list[BranchLifecycleMarker]] = {}
    for marker in markers:
        lifecycle_markers.setdefault(marker.branch, []).append(marker)
    default_ref = f"refs/remotes/{remote}/{default_branch}"
    default_revision = _revision(root, default_ref)
    branches = _unmerged_branches(
        root,
        remote=remote,
        default_branch=default_branch,
        default_revision=default_revision,
        review_heads=review_heads,
        disposition_branches=disposition_branches,
        lifecycle_markers=lifecycle_markers,
    )
    resolved_branches = {
        str(item["name"]): str(item["revision"])
        for item in branches
        if item["location"] == "local" and not item["reasons"]
    }
    # Merged topics are deliberately absent from the unmerged projection. Bind
    # their exact local heads too; a name alone cannot resolve a moving worktree.
    for line in _git_lines(
        root,
        "for-each-ref",
        f"--merged={default_revision}",
        "--format=%(refname:strip=2)%09%(objectname)",
        "refs/heads",
    ):
        name, separator, revision = line.partition("\t")
        if not separator or not name or not revision:
            raise ReleaseContributionsError(
                "release.contributions_git_failed", "Git returned an invalid merged ref"
            )
        resolved_branches[name] = revision
    worktrees = _worktrees(root, resolved_branches, default_branch=default_branch)
    if _revision(root, default_ref) != default_revision:
        raise ReleaseContributionsError(
            "release.contributions_git_changed",
            "remote default reference changed during contribution inventory",
        )
    unclassified = [
        {"kind": item["kind"], "number": item["number"], "reasons": item["reasons"]}
        for item in (*issues, *reviews)
        if item["reasons"]
    ]
    unclassified.extend(
        {"kind": "branch", "name": item["name"], "reasons": item["reasons"]}
        for item in branches
        if item["reasons"]
    )
    unclassified.extend(
        {
            "kind": "branch-lifecycle",
            "ref": item.get("ref"),
            "reasons": item.get("reasons", ["invalid-marker"]),
        }
        for item in invalid_markers
    )
    unclassified.extend(
        {"kind": "worktree", "path": item["path"], "reasons": item["reasons"]}
        for item in worktrees
        if item["reasons"]
    )
    in_scope_open = [
        {"kind": item["kind"], "number": item["number"], "url": item["url"]}
        for item in (*issues, *reviews)
        if not item["reasons"] and item["decision"] == "include"
    ]
    result: dict[str, object] = {
        "schema": RELEASE_CONTRIBUTIONS_SCHEMA,
        "release": release,
        "current_milestone": current_milestone,
        "forge": forge,
        "repository": slug,
        "default_branch": default_branch,
        "issues": issues,
        "reviews": reviews,
        "branches": branches,
        "branch_lifecycle": {
            "markers": [
                {**item.to_dict(), "ref": item.ref, "object": item.object_name}
                for item in markers
            ],
            "invalid": list(invalid_markers),
        },
        "worktrees": worktrees,
        "unclassified": unclassified,
        "in_scope_open": in_scope_open,
        "ready": not unclassified and not in_scope_open,
    }
    result["identity"] = canonical_identity(result).uri
    return result


def require_release_contributions_ready(result: Mapping[str, object]) -> None:
    unclassified = result.get("unclassified")
    in_scope = result.get("in_scope_open")
    if isinstance(unclassified, list) and unclassified:
        raise ReleaseContributionsError(
            "release.contributions_unclassified",
            f"{len(unclassified)} release contributions require disposition",
        )
    if isinstance(in_scope, list) and in_scope:
        raise ReleaseContributionsError(
            "release.contributions_in_scope_open",
            f"{len(in_scope)} current-release contributions remain open",
        )


def disposition_release_contribution(
    path: Path,
    *,
    kind: str,
    number: int,
    release: str,
    decision: str,
    milestone: str,
    reason: str,
    branches: Sequence[str] = (),
    authorize_external_write: bool,
    run_command: CommandRunner | None = None,
) -> dict[str, object]:
    """Set a forge milestone and append the canonical disposition comment."""

    if not authorize_external_write:
        raise ReleaseContributionsError(
            "release.external_authorization_required",
            "contribution disposition requires --authorize-external-write",
        )
    if kind not in {"issue", "review"} or number < 1:
        raise ReleaseContributionsError(
            "release.contributions_target_invalid",
            "target must be a positive issue or review number",
        )
    body = render_release_disposition(
        release=release,
        decision=decision,
        milestone=milestone,
        reason=reason,
        branches=branches,
    )
    try:
        inspect = inspect_project_tracker(path)
    except ProjectTrackerError as exc:
        raise ReleaseContributionsError(exc.code, exc.message) from exc
    forge = inspect.get("forge")
    root = Path(str(inspect["git_root"]))
    slug = _repository_slug(inspect.get("url"))
    runner = run_command or _run
    if forge == "github":
        noun = "issue" if kind == "issue" else "pr"
        commands = (
            (
                "gh",
                noun,
                "edit",
                str(number),
                "--repo",
                slug,
                "--milestone",
                milestone,
            ),
            ("gh", noun, "comment", str(number), "--repo", slug, "--body", body),
        )
    elif forge == "gitlab":
        project = urllib.parse.quote(slug, safe="")
        milestones = _completed_json(
            runner,
            ("glab", "api", f"projects/{project}/milestones?state=all&per_page=100"),
            root,
            code="release.contributions_milestones_invalid",
        )
        match = (
            next(
                (
                    item
                    for item in milestones
                    if isinstance(item, Mapping) and item.get("title") == milestone
                ),
                None,
            )
            if isinstance(milestones, list)
            else None
        )
        milestone_id = match.get("id") if isinstance(match, Mapping) else None
        if not isinstance(milestone_id, int):
            raise ReleaseContributionsError(
                "release.contributions_milestone_missing",
                f"GitLab milestone does not exist: {milestone}",
            )
        resource = "issues" if kind == "issue" else "merge_requests"
        commands = (
            (
                "glab",
                "api",
                "--method",
                "PUT",
                f"projects/{project}/{resource}/{number}",
                "-f",
                f"milestone_id={milestone_id}",
            ),
            (
                "glab",
                "api",
                "--method",
                "POST",
                f"projects/{project}/{resource}/{number}/notes",
                "-f",
                f"body={body}",
            ),
        )
    else:
        raise ReleaseContributionsError(
            "release.contributions_tracker_unsupported",
            f"cannot disposition contributions on {forge}",
        )
    for command in commands:
        try:
            completed = runner(command, root, _TRACKER_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseContributionsError(
                "release.contributions_tracker_unavailable",
                "tracker disposition command is unavailable",
            ) from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()[:512]
            raise ReleaseContributionsError(
                "release.contributions_write_failed",
                f"tracker disposition failed: {detail}",
            )
    result: dict[str, object] = {
        "schema": RELEASE_DISPOSITION_SCHEMA,
        "forge": forge,
        "repository": slug,
        "kind": kind,
        "number": number,
        "release": release,
        "decision": decision,
        "milestone": milestone,
        "branches": sorted(set(branches)),
    }
    result["identity"] = canonical_identity(result).uri
    return result


__all__ = [
    "RELEASE_CONTRIBUTIONS_SCHEMA",
    "RELEASE_DISPOSITION_MARKER",
    "RELEASE_DISPOSITION_SCHEMA",
    "ReleaseContributionsError",
    "disposition_release_contribution",
    "parse_release_disposition",
    "render_release_disposition",
    "require_release_contributions_ready",
    "sweep_release_contributions",
]
