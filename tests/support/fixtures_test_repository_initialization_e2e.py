"""Shared test fixtures extracted from test_repository_initialization_e2e."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def commit_project(root: Path) -> None:
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Repository DAG Test")
    git(root, "config", "user.email", "repository-dag@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", f"Create {root.name}")
