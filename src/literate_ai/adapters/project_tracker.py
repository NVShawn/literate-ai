"""Read Git remotes from a working tree and classify the issue tracker."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.application.project_tracker import (
    GitRemote,
    exact_git_revision,
    inspect_remotes,
)
from literate_ai.projects import PROJECT_FILENAME, discover_project

_GIT_TIMEOUT_SECONDS = 30


class ProjectTrackerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def inspect_project_tracker(path: Path) -> dict[str, object]:
    """Classify GitHub vs GitLab from Git remotes and name ``gh`` or ``glab``."""

    root = _git_work_tree(path)
    remotes = _list_remotes(root)
    inspect = inspect_remotes(remotes, revision=_head_revision(root))
    if inspect.forge == "ambiguous":
        raise ProjectTrackerError(
            "project.tracker_host_ambiguous",
            "Git remotes name both GitHub and GitLab; set origin to the tracker host",
        )
    payload = inspect.to_dict()
    project = discover_project(path)
    payload["project_root"] = str(project.root if project is not None else root)
    payload["git_root"] = str(root)
    payload["has_project"] = project is not None
    if project is not None:
        payload["project_file"] = PROJECT_FILENAME
    return payload


def _head_revision(root: Path) -> str | None:
    """Resolve an existing checkout HEAD to the immutable commit CI must inspect."""

    try:
        result = _run_git(
            ("git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"),
        )
    except FileNotFoundError as exc:
        raise ProjectTrackerError(
            "project.tracker_git_unavailable",
            "git is required to resolve the checkout commit for CI inspection",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectTrackerError(
            "project.tracker_git_timeout",
            "the checkout commit could not be resolved before the tracker deadline",
        ) from exc
    if result.returncode != 0:
        return None
    try:
        revision = exact_git_revision(
            result.stdout.decode("ascii", errors="strict").strip()
        )
    except (UnicodeError, ValueError) as exc:
        raise ProjectTrackerError(
            "project.tracker_git_output_invalid",
            "git reported an invalid checkout commit",
        ) from exc
    return revision


def _git_work_tree(path: Path) -> Path:
    selected = path.expanduser().resolve()
    try:
        result = _run_git(
            ("git", "-C", str(selected), "rev-parse", "--show-toplevel"),
        )
    except FileNotFoundError as exc:
        raise ProjectTrackerError(
            "project.tracker_git_unavailable",
            "git is required to read repository remotes from .git/config",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectTrackerError(
            "project.tracker_git_timeout",
            "git remotes could not be read before the tracker inspect deadline",
        ) from exc
    if result.returncode != 0:
        raise ProjectTrackerError(
            "project.tracker_not_a_git_repository",
            "tracker inspect requires a Git working tree so remotes can be read",
        )
    try:
        text = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise ProjectTrackerError(
            "project.tracker_git_output_invalid",
            "git reported a non-UTF-8 working tree path",
        ) from exc
    if not text:
        raise ProjectTrackerError(
            "project.tracker_not_a_git_repository",
            "tracker inspect requires a Git working tree so remotes can be read",
        )
    return Path(text)


def _list_remotes(root: Path) -> tuple[GitRemote, ...]:
    try:
        result = _run_git(
            ("git", "-C", str(root), "config", "--get-regexp", r"^remote\..+\.url$"),
        )
    except FileNotFoundError as exc:
        raise ProjectTrackerError(
            "project.tracker_git_unavailable",
            "git is required to read repository remotes from .git/config",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProjectTrackerError(
            "project.tracker_git_timeout",
            "git remotes could not be read before the tracker inspect deadline",
        ) from exc
    if result.returncode not in {0, 1}:
        raise ProjectTrackerError(
            "project.tracker_git_failed",
            "git config could not list remote URLs",
        )
    remotes: list[GitRemote] = []
    try:
        text = result.stdout.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ProjectTrackerError(
            "project.tracker_git_output_invalid",
            "git reported a non-UTF-8 remote URL",
        ) from exc
    for line in text.splitlines():
        key, separator, url = line.partition(" ")
        if not separator or not url.strip():
            continue
        prefix = "remote."
        suffix = ".url"
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        name = key[len(prefix) : -len(suffix)]
        if name:
            remotes.append(GitRemote(name=name, url=url.strip()))
    remotes.sort(key=lambda remote: remote.name)
    return tuple(remotes)


def _run_git(command: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    return run_with_tree_kill(
        command,
        env=_git_environment(),
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    return environment
