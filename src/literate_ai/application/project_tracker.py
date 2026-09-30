"""Classify a project's Git remotes as GitHub, GitLab, or unsupported."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse, urlunparse

Forge = Literal["github", "gitlab"]
TrackerHost = Forge | Literal["unsupported", "ambiguous"]

TRACKER_INSPECT_SCHEMA = "literate-ai/project-tracker-inspect@1"

_GITHUB_LIST_ISSUES = ("gh", "issue", "list", "--state", "open")
_GITHUB_LIST_PRS = ("gh", "pr", "list", "--state", "open")
_GITHUB_SEARCH_ISSUES = (
    "gh",
    "issue",
    "list",
    "--state",
    "all",
    "--search",
    "QUERY",
    "--json",
    "number,title,url,state",
)
_GITHUB_CREATE_ISSUE = (
    "gh",
    "issue",
    "create",
    "--title",
    "TITLE",
    "--body-file",
    "BODY_FILE",
)
_GITHUB_LAND_CREATE = ("gh", "pr", "create")
_GITHUB_LAND_MERGE = ("gh", "pr", "merge", "--merge")
_GITHUB_REVIEW_STATUS = (
    "gh",
    "pr",
    "list",
    "--state",
    "open",
    "--json",
    "number,title,body,baseRefName,headRefName,headRefOid,labels,author,updatedAt,"
    "url,isDraft,mergeable,statusCheckRollup",
)
_GITHUB_ISSUE_STATUS = (
    "gh",
    "issue",
    "list",
    "--state",
    "open",
    "--json",
    "number,title,url",
)
_GITLAB_LIST_ISSUES = ("glab", "issue", "list")
_GITLAB_LIST_MRS = ("glab", "mr", "list")
_GITLAB_SEARCH_ISSUES = ("glab", "issue", "list", "--search", "QUERY", "--all")
_GITLAB_CREATE_ISSUE = (
    "glab",
    "issue",
    "create",
    "--title",
    "TITLE",
    "--description-file",
    "BODY_FILE",
)
_GITLAB_CI_STATUS = ("glab", "ci", "status")
_GITLAB_LAND_CREATE = ("glab", "mr", "create")
_GITLAB_LAND_MERGE = ("glab", "mr", "merge")


@dataclass(frozen=True, slots=True)
class GitRemote:
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class TrackerInspect:
    forge: TrackerHost
    cli: str | None
    remote: str | None
    url: str | None
    review_noun: str | None
    issue_list: tuple[str, ...]
    review_list: tuple[str, ...]
    ci_status: tuple[str, ...] = ()
    land_create: tuple[str, ...] = ()
    land_merge: tuple[str, ...] = ()
    review_status: tuple[str, ...] = ()
    issue_status: tuple[str, ...] = ()
    issue_search: tuple[str, ...] = ()
    issue_create: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": TRACKER_INSPECT_SCHEMA,
            "forge": self.forge,
            "cli": self.cli,
            "remote": self.remote,
            "url": self.url,
            "review_noun": self.review_noun,
            "issue_list": list(self.issue_list),
            "review_list": list(self.review_list),
            "ci_status": list(self.ci_status),
            "land_create": list(self.land_create),
            "land_merge": list(self.land_merge),
            "review_status": list(self.review_status),
            "issue_status": list(self.issue_status),
            "issue_search": list(self.issue_search),
            "issue_create": list(self.issue_create),
        }


def sanitize_git_url(url: str) -> str:
    """Return ``url`` with userinfo stripped so tokens never enter evidence."""

    cleaned = url.strip()
    if "://" not in cleaned:
        if "@" in cleaned and ":" in cleaned:
            _userinfo, separator, remainder = cleaned.partition("@")
            if separator:
                return remainder
        return cleaned
    parsed = urlparse(cleaned)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = host
    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, "")
    )


def remote_host(url: str) -> str | None:
    """Return the lowercase hostname of a Git remote URL, or ``None``."""

    cleaned = url.strip()
    if not cleaned:
        return None
    if "://" not in cleaned:
        if cleaned.startswith("git@") or (
            "@" in cleaned and ":" in cleaned and not cleaned.startswith("/")
        ):
            left, separator, _path = cleaned.partition(":")
            if not separator:
                return None
            host = left.rsplit("@", 1)[-1].strip().strip("[]")
            return host.casefold() or None
        return None
    parsed = urlparse(cleaned)
    host = parsed.hostname
    if host is None:
        return None
    return unquote(host).casefold()


def classify_remote_host(host: str) -> TrackerHost:
    """Classify a hostname as GitHub, GitLab, ambiguous, or unsupported.

    A DNS label matches when it equals ``github`` / ``gitlab`` or contains that
    token (``gitlab.internal.example``, ``gist.github.com``). Hosts that match
    both forges in one name are ambiguous.
    """

    labels = tuple(part for part in host.casefold().split(".") if part)
    if not labels:
        return "unsupported"
    has_github = any("github" in label for label in labels)
    has_gitlab = any("gitlab" in label for label in labels)
    if has_github and has_gitlab:
        return "ambiguous"
    if has_github:
        return "github"
    if has_gitlab:
        return "gitlab"
    return "unsupported"


def classify_git_remote_url(url: str) -> TrackerHost:
    host = remote_host(url)
    if host is None:
        return "unsupported"
    return classify_remote_host(host)


def select_tracker_remote(remotes: tuple[GitRemote, ...]) -> GitRemote | None:
    """Prefer ``origin``, otherwise the sole remote, otherwise ``None``."""

    if not remotes:
        return None
    for remote in remotes:
        if remote.name == "origin":
            return remote
    if len(remotes) == 1:
        return remotes[0]
    return None


def exact_git_revision(revision: str) -> str:
    """Validate and return an immutable Git commit object ID."""

    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision) is None:
        raise ValueError(
            "Git revision must be a lowercase 40- or 64-character object ID"
        )
    return revision


def _github_ci_status(revision: str | None) -> tuple[str, ...]:
    """Return a CI query bound to one immutable Git commit, or no query."""

    if revision is None:
        return ()
    revision = exact_git_revision(revision)
    return (
        "gh",
        "run",
        "list",
        "--commit",
        revision,
        "--json",
        "databaseId,status,conclusion,name,url,headBranch",
    )


def inspect_remotes(
    remotes: tuple[GitRemote, ...], *, revision: str | None = None
) -> TrackerInspect:
    """Classify tracker host from Git remotes without invoking ``gh`` or ``glab``."""

    empty = TrackerInspect(
        forge="unsupported",
        cli=None,
        remote=None,
        url=None,
        review_noun=None,
        issue_list=(),
        review_list=(),
    )
    if not remotes:
        return empty
    selected = select_tracker_remote(remotes)
    if selected is None:
        forges = {classify_git_remote_url(remote.url) for remote in remotes}
        forges.discard("unsupported")
        if len(forges) == 1:
            (forge,) = tuple(forges)
            if forge in {"github", "gitlab"}:
                selected = remotes[0]
            else:
                return empty
        elif len(forges) > 1:
            return TrackerInspect(
                forge="ambiguous",
                cli=None,
                remote=None,
                url=None,
                review_noun=None,
                issue_list=(),
                review_list=(),
            )
        else:
            return empty
    forge = classify_git_remote_url(selected.url)
    sanitized = sanitize_git_url(selected.url)
    if forge == "github":
        return TrackerInspect(
            forge="github",
            cli="gh",
            remote=selected.name,
            url=sanitized,
            review_noun="pull-request",
            issue_list=_GITHUB_LIST_ISSUES,
            review_list=_GITHUB_LIST_PRS,
            ci_status=_github_ci_status(revision),
            land_create=_GITHUB_LAND_CREATE,
            land_merge=_GITHUB_LAND_MERGE,
            review_status=_GITHUB_REVIEW_STATUS,
            issue_status=_GITHUB_ISSUE_STATUS,
            issue_search=_GITHUB_SEARCH_ISSUES,
            issue_create=_GITHUB_CREATE_ISSUE,
        )
    if forge == "gitlab":
        return TrackerInspect(
            forge="gitlab",
            cli="glab",
            remote=selected.name,
            url=sanitized,
            review_noun="merge-request",
            issue_list=_GITLAB_LIST_ISSUES,
            review_list=_GITLAB_LIST_MRS,
            ci_status=_GITLAB_CI_STATUS,
            land_create=_GITLAB_LAND_CREATE,
            land_merge=_GITLAB_LAND_MERGE,
            issue_search=_GITLAB_SEARCH_ISSUES,
            issue_create=_GITLAB_CREATE_ISSUE,
        )
    return TrackerInspect(
        forge=forge,
        cli=None,
        remote=selected.name,
        url=sanitized,
        review_noun=None,
        issue_list=(),
        review_list=(),
    )


_RELEASE_TRAILER = re.compile(r"^Literate-AI-Release: (none|[0-9]+\.[0-9]+)$")


def classify_release_relevance(
    body: object, repository_policy: object | None
) -> tuple[str, str | None]:
    """Return the exact release trailer value and its repository-policy relevance."""

    if not isinstance(body, str):
        return "unknown", None
    reserved = [line for line in body.splitlines() if "Literate-AI-Release:" in line]
    if len(reserved) != 1:
        return "unknown", None
    match = _RELEASE_TRAILER.fullmatch(reserved[0])
    if match is None:
        return "unknown", None
    value = match.group(1)
    main_state = _policy_value(repository_policy, "main_state")
    if value == "none":
        return ("none" if main_state == "free" else "unknown"), value
    pre_release = _policy_value(repository_policy, "pre_release_version")
    expected = _major_minor(pre_release)
    if expected is None:
        return "unknown", value
    if main_state != "pre-release":
        return "unknown", value
    return ("current" if value == expected else "other"), value


def _policy_value(policy: object | None, name: str) -> str | None:
    value: Any
    if isinstance(policy, Mapping):
        value = policy.get(name)
    else:
        value = getattr(policy, name, None)
    return value if isinstance(value, str) and value else None


def _major_minor(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?(?:[-+].*)?", value)
    return f"{match.group(1)}.{match.group(2)}" if match else None
