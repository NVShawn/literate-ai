"""Bounded nonexecuting local object capture for physical before-file planning."""

from __future__ import annotations

import os
import time
from pathlib import Path

from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory
from literate_ai.contracts.repository_refresh import RepositoryRefreshTarget
from literate_ai.contracts.repository_tree import (
    RepositoryTreeCapturePolicy,
    RepositoryTreeSnapshot,
)

from ._repository_tree_capture import capture_repository_tree
from .builders._process import run_bounded_process
from .builders.python import BuildError
from .repository_orchestration import OrchestrationInventoryError


def capture_local_repository_tree(
    root: Path, commit: str, *, policy: RepositoryTreeCapturePolicy
) -> RepositoryTreeSnapshot:
    """Read exact old objects, not publication evidence or physical rollback bytes."""
    RepositoryRefreshTarget("before-tree", commit)
    if not isinstance(policy, RepositoryTreeCapturePolicy):
        raise TypeError("local tree capture requires a typed bounds policy")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_ALLOW_PROTOCOL="",
    )
    deadline = time.monotonic() + 120

    def run(*arguments, stdout_limit_bytes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OrchestrationInventoryError(
                "refresh_before_timeout", "local tree capture exceeded its deadline"
            )
        result = run_bounded_process(
            (
                "git",
                "--no-pager",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                *arguments,
            ),
            cwd=root,
            environment=environment,
            timeout_seconds=min(remaining, 30),
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=64 * 1024,
            error_prefix="orchestration.refresh_before",
        )
        if result.returncode:
            raise OrchestrationInventoryError(
                "refresh_before_unavailable",
                "local before-tree objects are unavailable",
            )
        if time.monotonic() >= deadline:
            raise OrchestrationInventoryError(
                "refresh_before_timeout", "local tree capture exceeded its deadline"
            )
        return result

    try:
        require_safe_directory(root)
        result = capture_repository_tree(run, commit, policy)
        require_safe_directory(root)
        return result
    except OrchestrationInventoryError:
        raise
    except (BuildError, OSError, UnsafeFilesystemPathError):
        raise OrchestrationInventoryError(
            "refresh_before_unavailable", "bounded local tree capture failed"
        ) from None
