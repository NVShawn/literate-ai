"""Checkout a parent repository under parents/<id> in the current project."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import BuildError
from literate_ai.application.project_tracker import remote_host, sanitize_git_url
from literate_ai.contracts import RepositoryFetchDeadlinePolicy
from literate_ai.projects import discover_project

_GIT_FILE_PROTOCOL = ("-c", "protocol.file.allow=always")
_PARENT_ROOT = "parents"
_SAFE_ID = re.compile(r"[^a-z0-9-]+")
_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class ParentCheckoutError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _ParentGitRuntime:
    policy: RepositoryFetchDeadlinePolicy
    provenance: str
    field_provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provenance not in {"framework-default", "cli", "project-policy"}:
            raise ValueError("Git deadline provenance is invalid")
        expected = {"total_seconds", "no_progress_seconds", "connect_seconds"}
        if self.field_provenance and (
            set(self.field_provenance) != expected
            or any(
                value not in {"framework-default", "cli", "project-policy"}
                for value in self.field_provenance.values()
            )
        ):
            raise ValueError("Git deadline field provenance is invalid")

    @property
    def evidence(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/repository-fetch-deadline-selection@1",
            "policy": self.policy.to_dict(),
            "policy_identity": self.policy.identity.uri,
            "provenance": self.provenance,
            "field_provenance": (
                dict(self.field_provenance)
                if self.field_provenance
                else {
                    name: self.provenance
                    for name in (
                        "total_seconds",
                        "no_progress_seconds",
                        "connect_seconds",
                    )
                }
            ),
        }

    def git(self, cwd: Path, *arguments: str) -> None:
        self.run(("git", *_GIT_FILE_PROTOCOL, *arguments), cwd=cwd)

    def text(self, cwd: Path, *arguments: str) -> str:
        result = self.run(("git", *_GIT_FILE_PROTOCOL, *arguments), cwd=cwd)
        try:
            return result.stdout.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ParentCheckoutError(
                "project.parent_git_output_invalid",
                "git reported non-UTF-8 output",
            ) from exc

    def run(
        self, command: tuple[str, ...] | list[str], *, cwd: Path
    ) -> subprocess.CompletedProcess[bytes]:
        argv = list(command)
        try:
            completed = run_bounded_process(
                argv,
                cwd=cwd,
                environment=_git_environment(),
                timeout_seconds=self.policy.total_seconds,
                inactivity_timeout_seconds=self.policy.no_progress_seconds,
                stdout_limit_bytes=1024 * 1024,
                stderr_limit_bytes=1024 * 1024,
                error_prefix="project.parent_git",
            )
        except BuildError as exc:
            if exc.code.endswith("launch_failed"):
                raise ParentCheckoutError(
                    "project.parent_git_unavailable",
                    "git is required to check out a parent repository",
                ) from exc
            if exc.code.endswith("timeout") or exc.code.endswith("no_progress_timeout"):
                raise ParentCheckoutError(
                    "project.parent_git_timeout",
                    "parent checkout exceeded its Git deadline "
                    f"(total_seconds={self.policy.total_seconds}, "
                    f"no_progress_seconds={self.policy.no_progress_seconds}, "
                    f"provenance={self.provenance})",
                ) from exc
            if exc.code.endswith("output_stream"):
                raise ParentCheckoutError(
                    "project.parent_git_output_stream",
                    "git descendants left inherited output streams open",
                ) from exc
            raise ParentCheckoutError(
                "project.parent_git_failed",
                "git failed during parent checkout",
            ) from exc
        result = subprocess.CompletedProcess(
            argv, completed.returncode, completed.stdout, completed.stderr
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ParentCheckoutError(
                "project.parent_git_failed",
                detail or "git failed during parent checkout",
            )
        return result


def checkout_parent(
    path: Path,
    source: str,
    *,
    deadline_policy: RepositoryFetchDeadlinePolicy | None = None,
    deadline_provenance: str = "framework-default",
    deadline_field_provenance: dict[str, str] | None = None,
) -> dict[str, object]:
    """Clone or update a parent under ``parents/<id>`` with submodules and LFS."""

    git = _ParentGitRuntime(
        deadline_policy or RepositoryFetchDeadlinePolicy(),
        deadline_provenance,
        {} if deadline_field_provenance is None else deadline_field_provenance,
    )
    root = _require_git_root(path, git)
    url, revision = _parse_source(source)
    identity = parent_prefix_id(url)
    prefix = root / _PARENT_ROOT / identity
    if prefix.exists() and not prefix.is_dir():
        raise ParentCheckoutError(
            "project.parent_prefix_invalid",
            f"parent checkout prefix is not a directory: {prefix.as_posix()}",
        )
    if prefix.exists() and (prefix / ".git").exists():
        action = "updated"
        git.git(prefix, "remote", "set-url", "origin", url)
        if revision == "HEAD":
            git.git(prefix, "pull", "--ff-only", "--progress", "--recurse-submodules")
        else:
            git.git(prefix, "fetch", "--progress", "--prune", "origin", revision)
            git.git(
                prefix,
                "checkout",
                "--quiet",
                "--recurse-submodules",
                "--detach",
                "FETCH_HEAD",
            )
        git.git(prefix, "submodule", "update", "--progress", "--init", "--recursive")
    else:
        if prefix.exists():
            raise ParentCheckoutError(
                "project.parent_prefix_occupied",
                "parent checkout prefix exists but is not a Git checkout",
            )
        prefix.parent.mkdir(parents=True, exist_ok=True)
        clone = [
            "git",
            *_GIT_FILE_PROTOCOL,
            "clone",
            "--progress",
            "--recurse-submodules",
            url,
            str(prefix),
        ]
        git.run(clone, cwd=root)
        git.git(prefix, "checkout", "--quiet", revision)
        git.git(prefix, "submodule", "update", "--progress", "--init", "--recursive")
        action = "added"
    lfs = _pull_lfs(prefix, git)
    _exclude_parents_prefix(root, git)
    project = discover_project(path)
    return {
        "schema": "literate-ai/parent-checkout@1",
        "action": action,
        "prefix": prefix.relative_to(root).as_posix(),
        "url": sanitize_git_url(url),
        "revision": git.text(prefix, "rev-parse", "HEAD").strip(),
        "requested_revision": revision,
        "submodules": (prefix / ".gitmodules").is_file(),
        "lfs": lfs,
        "project_root": str(project.root if project is not None else root),
        "repository_fetch_deadline": git.evidence,
    }


def parent_prefix_id(url: str) -> str:
    """Stable short directory name for a parent URL."""

    cleaned = url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    parsed = urlparse(cleaned)
    path = parsed.path if parsed.scheme else cleaned.replace("\\", "/")
    if ":" in path and "@" in path and "://" not in url:
        path = path.split(":", 1)[-1]
    name = path.rstrip("/").rsplit("/", 1)[-1]
    identity = _SAFE_ID.sub("-", name.casefold()).strip("-")
    if not identity:
        raise ParentCheckoutError(
            "project.parent_prefix_invalid",
            "parent URL does not contain a usable repository name",
        )
    return identity[:32]


def _parse_source(source: str) -> tuple[str, str]:
    cleaned = source.strip()
    if not cleaned:
        raise ParentCheckoutError(
            "project.parent_source_invalid",
            "parent checkout requires a Git URL and optional #revision",
        )
    url, separator, revision = cleaned.partition("#")
    if not url:
        raise ParentCheckoutError(
            "project.parent_source_invalid",
            "parent checkout requires a Git URL and optional #revision",
        )
    if "://" in url:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            raise ParentCheckoutError(
                "project.parent_url_credentials",
                "parent checkout URLs must not contain passwords or tokens",
            )
    elif "@" in url and ":" in url:
        userinfo = url.split("@", 1)[0]
        if ":" in userinfo:
            raise ParentCheckoutError(
                "project.parent_url_credentials",
                "parent checkout URLs must not contain passwords or tokens",
            )
    host = remote_host(url)
    if host is None and "://" in url and urlparse(url).scheme not in {"file", "git"}:
        raise ParentCheckoutError(
            "project.parent_source_invalid",
            "parent checkout requires a Git URL",
        )
    return url, (revision if separator and revision else "HEAD")


def _pull_lfs(prefix: Path, git: _ParentGitRuntime) -> bool:
    if not _has_lfs_pointers(prefix):
        return False
    try:
        git.run(("git", "lfs", "pull"), cwd=prefix)
    except ParentCheckoutError as exc:
        if "git-lfs" in exc.message or exc.code == "project.parent_git_unavailable":
            raise ParentCheckoutError(
                "project.parent_lfs_unavailable",
                "parent checkout has Git LFS pointers but git-lfs is not available",
            ) from exc
        raise
    return True


def _has_lfs_pointers(prefix: Path) -> bool:
    attributes = prefix / ".gitattributes"
    if attributes.is_file():
        try:
            text = attributes.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if "filter=lfs" in text:
            return True
    for path in prefix.rglob("*"):
        if ".git" in path.parts:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            header = path.read_bytes()[: len(_LFS_POINTER)]
        except OSError:
            continue
        if header == _LFS_POINTER:
            return True
    return False


def _exclude_parents_prefix(root: Path, git: _ParentGitRuntime) -> None:
    exclude = root / ".git" / "info" / "exclude"
    if exclude.is_dir() or (root / ".git").is_file():
        # Worktrees keep info/exclude in the common dir; git itself still honors it
        # when written through `git -C root rev-parse --git-path info/exclude`.
        pass
    path_text = git.text(root, "rev-parse", "--git-path", "info/exclude").strip()
    exclude_path = Path(path_text)
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_PARENT_ROOT}/\n"
    existing = ""
    if exclude_path.is_file():
        existing = exclude_path.read_text(encoding="utf-8")
    entries = {item.strip() for item in existing.splitlines() if item.strip()}
    if f"{_PARENT_ROOT}/" not in entries and _PARENT_ROOT not in entries:
        exclude_path.write_text(existing + line, encoding="utf-8")


def _require_git_root(path: Path, git: _ParentGitRuntime) -> Path:
    selected = path.expanduser().resolve()
    try:
        text = git.text(selected, "rev-parse", "--show-toplevel").strip()
    except ParentCheckoutError:
        raise
    if not text:
        raise ParentCheckoutError(
            "project.parent_not_a_git_repository",
            "parent checkout requires a Git working tree in the current project",
        )
    return Path(text)


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    return environment
